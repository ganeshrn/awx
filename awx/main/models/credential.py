# Copyright (c) 2015 Ansible, Inc.
# All Rights Reserved.
from contextlib import nullcontext
import functools
import inspect
import logging
import os
from pkg_resources import iter_entry_points
import re
import stat
import tempfile
from types import SimpleNamespace

# Jinja2
from jinja2 import sandbox

# Django
from django.apps.config import AppConfig
from django.apps.registry import Apps
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.conf import settings
from django.utils.encoding import force_str
from django.utils.functional import cached_property
from django.utils.timezone import now
from django.contrib.auth.models import User

# DRF
from awx.main.utils.pglock import advisory_lock
from rest_framework.serializers import ValidationError as DRFValidationError

# AWX
from awx.api.versioning import reverse
from awx.main.fields import (
    ImplicitRoleField,
    CredentialInputField,
    CredentialTypeInputField,
    CredentialTypeInjectorField,
    DynamicCredentialInputField,
)
from awx.main.utils import decrypt_field, classproperty, set_environ
from awx.main.utils.safe_yaml import safe_dump
from awx.main.utils.execution_environments import to_container_path
from awx.main.validators import validate_ssh_private_key
from awx.main.models.base import CommonModelNameNotUnique, PasswordFieldsModel, PrimordialModel
from awx.main.models.mixins import ResourceMixin
from awx.main.models.rbac import (
    ROLE_SINGLETON_SYSTEM_ADMINISTRATOR,
    ROLE_SINGLETON_SYSTEM_AUDITOR,
)
from awx.main.models import Team, Organization
from awx.main.utils import encrypt_field
from awx_plugins.credentials import injectors as builtin_injectors

__all__ = ['Credential', 'CredentialType', 'CredentialInputSource', 'build_safe_env']

logger = logging.getLogger('awx.main.models.credential')
credential_plugins = dict((ep.name, ep.load()) for ep in iter_entry_points('awx_plugins.credentials'))

HIDDEN_PASSWORD = '**********'


def build_safe_env(env):
    """
    Build environment dictionary, hiding potentially sensitive information
    such as passwords or keys.
    """
    hidden_re = re.compile(r'API|TOKEN|KEY|SECRET|PASS', re.I)
    urlpass_re = re.compile(r'^.*?://[^:]+:(.*?)@.*?$')
    safe_env = dict(env)
    for k, v in safe_env.items():
        if k == 'AWS_ACCESS_KEY_ID':
            continue
        elif k.startswith('ANSIBLE_') and not k.startswith('ANSIBLE_NET') and not k.startswith('ANSIBLE_GALAXY_SERVER'):
            continue
        elif hidden_re.search(k):
            safe_env[k] = HIDDEN_PASSWORD
        elif type(v) == str and urlpass_re.match(v):
            safe_env[k] = urlpass_re.sub(HIDDEN_PASSWORD, v)
    return safe_env


class Credential(PasswordFieldsModel, CommonModelNameNotUnique, ResourceMixin):
    """
    A credential contains information about how to talk to a remote resource
    Usually this is a SSH key location, and possibly an unlock password.
    If used with sudo, a sudo password should be set if required.
    """

    class Meta:
        app_label = 'main'
        ordering = ('name',)
        unique_together = ('organization', 'name', 'credential_type')
        permissions = [('use_credential', 'Can use credential in a job or related resource')]

    PASSWORD_FIELDS = ['inputs']
    FIELDS_TO_PRESERVE_AT_COPY = ['input_sources']

    credential_type = models.ForeignKey(
        'CredentialType',
        related_name='credentials',
        null=False,
        on_delete=models.CASCADE,
        help_text=_('Specify the type of credential you want to create. Refer to the documentation for details on each type.'),
    )
    managed = models.BooleanField(default=False, editable=False)
    organization = models.ForeignKey(
        'Organization',
        null=True,
        default=None,
        blank=True,
        on_delete=models.CASCADE,
        related_name='credentials',
    )
    inputs = CredentialInputField(
        blank=True, default=dict, help_text=_('Enter inputs using either JSON or YAML syntax. Refer to the documentation for example syntax.')
    )
    admin_role = ImplicitRoleField(
        parent_role=[
            'singleton:' + ROLE_SINGLETON_SYSTEM_ADMINISTRATOR,
            'organization.credential_admin_role',
        ],
    )
    use_role = ImplicitRoleField(
        parent_role=[
            'admin_role',
        ]
    )
    read_role = ImplicitRoleField(
        parent_role=[
            'singleton:' + ROLE_SINGLETON_SYSTEM_AUDITOR,
            'organization.auditor_role',
            'use_role',
            'admin_role',
        ]
    )

    @property
    def kind(self):
        return self.credential_type.namespace

    @property
    def cloud(self):
        return self.credential_type.kind == 'cloud'

    @property
    def kubernetes(self):
        return self.credential_type.kind == 'kubernetes'

    def get_absolute_url(self, request=None):
        return reverse('api:credential_detail', kwargs={'pk': self.pk}, request=request)

    #
    # TODO: the SSH-related properties below are largely used for validation
    # and for determining passwords necessary for job/ad-hoc launch
    #
    # These are SSH-specific; should we move them elsewhere?
    #
    @property
    def needs_ssh_password(self):
        return self.credential_type.kind == 'ssh' and self.inputs.get('password') == 'ASK'

    @property
    def has_encrypted_ssh_key_data(self):
        try:
            ssh_key_data = self.get_input('ssh_key_data')
        except AttributeError:
            return False

        try:
            pem_objects = validate_ssh_private_key(ssh_key_data)
            for pem_object in pem_objects:
                if pem_object.get('key_enc', False):
                    return True
        except ValidationError:
            pass
        return False

    @property
    def needs_ssh_key_unlock(self):
        if self.credential_type.kind == 'ssh' and self.inputs.get('ssh_key_unlock') in ('ASK', ''):
            return self.has_encrypted_ssh_key_data
        return False

    @property
    def needs_become_password(self):
        return self.credential_type.kind == 'ssh' and self.inputs.get('become_password') == 'ASK'

    @property
    def needs_vault_password(self):
        return self.credential_type.kind == 'vault' and self.inputs.get('vault_password') == 'ASK'

    @property
    def passwords_needed(self):
        needed = []
        for field in ('ssh_password', 'become_password', 'ssh_key_unlock'):
            if getattr(self, 'needs_%s' % field):
                needed.append(field)
        if self.needs_vault_password:
            if self.inputs.get('vault_id'):
                needed.append('vault_password.{}'.format(self.inputs.get('vault_id')))
            else:
                needed.append('vault_password')
        return needed

    @cached_property
    def dynamic_input_fields(self):
        # if the credential is not yet saved we can't access the input_sources
        if not self.id:
            return []
        return [obj.input_field_name for obj in self.input_sources.all()]

    def _password_field_allows_ask(self, field):
        return field in self.credential_type.askable_fields

    def save(self, *args, **kwargs):
        self.PASSWORD_FIELDS = self.credential_type.secret_fields

        if self.pk:
            cred_before = Credential.objects.get(pk=self.pk)
            inputs_before = cred_before.inputs
            # Look up the currently persisted value so that we can replace
            # $encrypted$ with the actual DB-backed value
            for field in self.PASSWORD_FIELDS:
                if self.inputs.get(field) == '$encrypted$':
                    self.inputs[field] = inputs_before[field]

        super(Credential, self).save(*args, **kwargs)

    def mark_field_for_save(self, update_fields, field):
        if 'inputs' not in update_fields:
            update_fields.append('inputs')

    def encrypt_field(self, field, ask):
        if field not in self.inputs:
            return None
        encrypted = encrypt_field(self, field, ask=ask)
        if encrypted:
            self.inputs[field] = encrypted
        elif field in self.inputs:
            del self.inputs[field]

    def display_inputs(self):
        field_val = self.inputs.copy()
        for k, v in field_val.items():
            if force_str(v).startswith('$encrypted$'):
                field_val[k] = '$encrypted$'
        return field_val

    def unique_hash(self, display=False):
        """
        Credential exclusivity is not defined solely by the related
        credential type (due to vault), so this produces a hash
        that can be used to evaluate exclusivity
        """
        if display:
            type_alias = self.credential_type.name
        else:
            type_alias = self.credential_type_id
        if self.credential_type.kind == 'vault' and self.has_input('vault_id'):
            if display:
                fmt_str = '{} (id={})'
            else:
                fmt_str = '{}_{}'
            return fmt_str.format(type_alias, self.get_input('vault_id'))
        return str(type_alias)

    @staticmethod
    def unique_dict(cred_qs):
        ret = {}
        for cred in cred_qs:
            ret[cred.unique_hash()] = cred
        return ret

    def get_input(self, field_name, **kwargs):
        """
        Get an injectable and decrypted value for an input field.

        Retrieves the value for a given credential input field name. Return
        values for secret input fields are decrypted. If the credential doesn't
        have an input value defined for the given field name, an AttributeError
        is raised unless a default value is provided.

        :param field_name(str):        The name of the input field.
        :param default(optional[str]): A default return value to use.
        """
        if self.credential_type.kind != 'external' and field_name in self.dynamic_input_fields:
            return self._get_dynamic_input(field_name)
        if field_name in self.credential_type.secret_fields:
            try:
                return decrypt_field(self, field_name)
            except AttributeError:
                for field in self.credential_type.inputs.get('fields', []):
                    if field['id'] == field_name and 'default' in field:
                        return field['default']
                if 'default' in kwargs:
                    return kwargs['default']
                raise AttributeError(field_name)
        if field_name in self.inputs:
            return self.inputs[field_name]
        if 'default' in kwargs:
            return kwargs['default']
        for field in self.credential_type.inputs.get('fields', []):
            if field['id'] == field_name and 'default' in field:
                return field['default']
        raise AttributeError(field_name)

    def has_input(self, field_name):
        if field_name in self.dynamic_input_fields:
            return True
        return field_name in self.inputs and self.inputs[field_name] not in ('', None)

    def has_inputs(self, field_names=()):
        for name in field_names:
            if not self.has_input(name):
                raise ValueError('{} is not an input field'.format(name))
        return True

    def _get_dynamic_input(self, field_name):
        for input_source in self.input_sources.all():
            if input_source.input_field_name == field_name:
                return input_source.get_input_value()
        else:
            raise ValueError('{} is not a dynamic input field'.format(field_name))

    def validate_role_assignment(self, actor, role_definition):
        if self.organization:
            if isinstance(actor, User):
                if actor.is_superuser or Organization.access_qs(actor, 'member').filter(id=self.organization.id).exists():
                    return
            if isinstance(actor, Team):
                if actor.organization == self.organization:
                    return
            raise DRFValidationError({'detail': _(f"You cannot grant credential access to a {actor._meta.object_name} not in the credentials' organization")})


class CredentialType(CommonModelNameNotUnique):
    CREDENTIAL_REGISTRATION_ADVISORY_LOCK_NAME = 'setup_tower_managed_defaults'

    """
    A reusable schema for a credential.

    Used to define a named credential type with fields (e.g., an API key) and
    output injectors (i.e., an environment variable that uses the API key).
    """

    class Meta:
        app_label = 'main'
        ordering = ('kind', 'name')
        unique_together = (('name', 'kind'),)

    KIND_CHOICES = (
        ('ssh', _('Machine')),
        ('vault', _('Vault')),
        ('net', _('Network')),
        ('scm', _('Source Control')),
        ('cloud', _('Cloud')),
        ('registry', _('Container Registry')),
        ('token', _('Personal Access Token')),
        ('insights', _('Insights')),
        ('external', _('External')),
        ('kubernetes', _('Kubernetes')),
        ('galaxy', _('Galaxy/Automation Hub')),
        ('cryptography', _('Cryptography')),
    )

    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    managed = models.BooleanField(default=False, editable=False)
    namespace = models.CharField(max_length=1024, null=True, default=None, editable=False)
    inputs = CredentialTypeInputField(
        blank=True, default=dict, help_text=_('Enter inputs using either JSON or YAML syntax. Refer to the documentation for example syntax.')
    )
    injectors = CredentialTypeInjectorField(
        blank=True,
        default=dict,
        help_text=_('Enter injectors using either JSON or YAML syntax. Refer to the documentation for example syntax.'),
    )

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super(CredentialType, cls).from_db(db, field_names, values)
        if instance.managed and instance.namespace:
            native = ManagedCredentialType.registry[instance.namespace]
            instance.inputs = native.inputs
            instance.injectors = native.injectors
        return instance

    def get_absolute_url(self, request=None):
        return reverse('api:credential_type_detail', kwargs={'pk': self.pk}, request=request)

    @property
    def defined_fields(self):
        return [field.get('id') for field in self.inputs.get('fields', [])]

    @property
    def secret_fields(self):
        return [field['id'] for field in self.inputs.get('fields', []) if field.get('secret', False) is True]

    @property
    def askable_fields(self):
        return [field['id'] for field in self.inputs.get('fields', []) if field.get('ask_at_runtime', False) is True]

    @property
    def plugin(self):
        if self.kind != 'external':
            raise AttributeError('plugin')
        [plugin] = [plugin for ns, plugin in credential_plugins.items() if ns == self.namespace]
        return plugin

    def default_for_field(self, field_id):
        for field in self.inputs.get('fields', []):
            if field['id'] == field_id:
                if 'choices' in field:
                    return field['choices'][0]
                return {'string': '', 'boolean': False}[field['type']]

    @classproperty
    def defaults(cls):
        return dict((k, functools.partial(v.create)) for k, v in ManagedCredentialType.registry.items())

    @classmethod
    def _get_credential_type_class(cls, apps: Apps = None, app_config: AppConfig = None):
        """
        Legacy code passing in apps while newer code should pass only the specific 'main' app config.
        """
        if apps and app_config:
            raise ValueError('Expected only apps or app_config to be defined, not both')

        if not any(
            (
                apps,
                app_config,
            )
        ):
            return CredentialType

        if apps:
            app_config = apps.get_app_config('main')

        return app_config.get_model('CredentialType')

    @classmethod
    def _setup_tower_managed_defaults(cls, apps: Apps = None, app_config: AppConfig = None):
        ct_class = cls._get_credential_type_class(apps=apps, app_config=app_config)
        for default in ManagedCredentialType.registry.values():
            existing = ct_class.objects.filter(name=default.name, kind=default.kind).first()
            if existing is not None:
                existing.namespace = default.namespace
                existing.inputs = {}
                existing.injectors = {}
                existing.save()
                continue
            logger.debug(_("adding %s credential type" % default.name))
            params = default.get_creation_params()
            if 'managed' not in [f.name for f in ct_class._meta.get_fields()]:
                params['managed_by_tower'] = params.pop('managed')
            params['created'] = params['modified'] = now()  # CreatedModifiedModel service
            created = ct_class(**params)
            created.inputs = created.injectors = {}
            created.save()

    @classmethod
    def setup_tower_managed_defaults(cls, apps: Apps = None, app_config: AppConfig = None, lock: bool = True, wait_for_lock: bool = False):
        """
        Create a CredentialType for discovered credential plugins.

        By default, this function will attempt to acquire the globally distributed lock (postgres advisory lock).
        If the lock is acquired the method will call the underlying method.
        If the lock is NOT acquired the method will NOT call the underlying method.

        lock=False will set acquired to True and appear to acquire the lock.
        lock=True, wait_for_lock=False will attempt to acquire the lock and NOT block.
        lock=True, wait_for_lock=True will attempt to acquire the lock and will block until the exclusive lock is acquired.

        :param lock(optional[bool]):          Attempt to acquire the postgres advisory lock.
        :param wait_for_lock(optional[bool]): Block and wait forever for the postgres advisory lock.
        """
        if apps and not apps.ready:
            return

        with advisory_lock(cls.CREDENTIAL_REGISTRATION_ADVISORY_LOCK_NAME, wait=wait_for_lock) if lock else nullcontext(True) as acquired:
            if acquired:
                cls._setup_tower_managed_defaults(apps=apps, app_config=app_config)

    @classmethod
    def load_plugin(cls, ns, plugin):
        ManagedCredentialType(namespace=ns, name=plugin.name, kind='external', inputs=plugin.inputs)

    def inject_credential(self, credential, env, safe_env, args, private_data_dir):
        """
        Inject credential data into the environment variables and arguments
        passed to `ansible-playbook`

        :param credential:       a :class:`awx.main.models.Credential` instance
        :param env:              a dictionary of environment variables used in
                                 the `ansible-playbook` call.  This method adds
                                 additional environment variables based on
                                 custom `env` injectors defined on this
                                 CredentialType.
        :param safe_env:         a dictionary of environment variables stored
                                 in the database for the job run
                                 (`UnifiedJob.job_env`); secret values should
                                 be stripped
        :param args:             a list of arguments passed to
                                 `ansible-playbook` in the style of
                                 `subprocess.call(args)`.  This method appends
                                 additional arguments based on custom
                                 `extra_vars` injectors defined on this
                                 CredentialType.
        :param private_data_dir: a temporary directory to store files generated
                                 by `file` injectors (like config files or key
                                 files)
        """
        if not self.injectors:
            if self.managed and credential.credential_type.namespace in dir(builtin_injectors):
                injected_env = {}
                getattr(builtin_injectors, credential.credential_type.namespace)(credential, injected_env, private_data_dir)
                env.update(injected_env)
                safe_env.update(build_safe_env(injected_env))
            return

        class TowerNamespace:
            pass

        tower_namespace = TowerNamespace()

        # maintain a normal namespace for building the ansible-playbook arguments (env and args)
        namespace = {'tower': tower_namespace}

        # maintain a sanitized namespace for building the DB-stored arguments (safe_env)
        safe_namespace = {'tower': tower_namespace}

        # build a normal namespace with secret values decrypted (for
        # ansible-playbook) and a safe namespace with secret values hidden (for
        # DB storage)
        injectable_fields = list(credential.inputs.keys()) + credential.dynamic_input_fields
        for field_name in list(set(injectable_fields)):
            value = credential.get_input(field_name)

            if type(value) is bool:
                # boolean values can't be secret/encrypted/external
                safe_namespace[field_name] = namespace[field_name] = value
                continue

            if field_name in self.secret_fields:
                safe_namespace[field_name] = '**********'
            elif len(value):
                safe_namespace[field_name] = value
            if len(value):
                namespace[field_name] = value

        for field in self.inputs.get('fields', []):
            # default missing boolean fields to False
            if field['type'] == 'boolean' and field['id'] not in credential.inputs.keys():
                namespace[field['id']] = safe_namespace[field['id']] = False
            # make sure private keys end with a \n
            if field.get('format') == 'ssh_private_key':
                if field['id'] in namespace and not namespace[field['id']].endswith('\n'):
                    namespace[field['id']] += '\n'

        file_tmpls = self.injectors.get('file', {})
        # If any file templates are provided, render the files and update the
        # special `tower` template namespace so the filename can be
        # referenced in other injectors

        sandbox_env = sandbox.ImmutableSandboxedEnvironment()

        for file_label, file_tmpl in file_tmpls.items():
            data = sandbox_env.from_string(file_tmpl).render(**namespace)
            _, path = tempfile.mkstemp(dir=os.path.join(private_data_dir, 'env'))
            with open(path, 'w') as f:
                f.write(data)
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            container_path = to_container_path(path, private_data_dir)

            # determine if filename indicates single file or many
            if file_label.find('.') == -1:
                tower_namespace.filename = container_path
            else:
                if not hasattr(tower_namespace, 'filename'):
                    tower_namespace.filename = TowerNamespace()
                file_label = file_label.split('.')[1]
                setattr(tower_namespace.filename, file_label, container_path)

        injector_field = self._meta.get_field('injectors')
        for env_var, tmpl in self.injectors.get('env', {}).items():
            try:
                injector_field.validate_env_var_allowed(env_var)
            except ValidationError as e:
                logger.error('Ignoring prohibited env var {}, reason: {}'.format(env_var, e))
                continue
            env[env_var] = sandbox_env.from_string(tmpl).render(**namespace)
            safe_env[env_var] = sandbox_env.from_string(tmpl).render(**safe_namespace)

        if 'INVENTORY_UPDATE_ID' not in env:
            # awx-manage inventory_update does not support extra_vars via -e
            def build_extra_vars(node):
                if isinstance(node, dict):
                    return {build_extra_vars(k): build_extra_vars(v) for k, v in node.items()}
                elif isinstance(node, list):
                    return [build_extra_vars(x) for x in node]
                else:
                    return sandbox_env.from_string(node).render(**namespace)

            def build_extra_vars_file(vars, private_dir):
                handle, path = tempfile.mkstemp(dir=os.path.join(private_dir, 'env'))
                f = os.fdopen(handle, 'w')
                f.write(safe_dump(vars))
                f.close()
                os.chmod(path, stat.S_IRUSR)
                return path

            extra_vars = build_extra_vars(self.injectors.get('extra_vars', {}))
            if extra_vars:
                path = build_extra_vars_file(extra_vars, private_data_dir)
                container_path = to_container_path(path, private_data_dir)
                args.extend(['-e', '@%s' % container_path])


class ManagedCredentialType(SimpleNamespace):
    registry = {}

    def __init__(self, namespace, **kwargs):
        for k in ('inputs', 'injectors'):
            if k not in kwargs:
                kwargs[k] = {}
        super(ManagedCredentialType, self).__init__(namespace=namespace, **kwargs)
        if namespace in ManagedCredentialType.registry:
            raise ValueError(
                'a ManagedCredentialType with namespace={} is already defined in {}'.format(
                    namespace, inspect.getsourcefile(ManagedCredentialType.registry[namespace].__class__)
                )
            )
        ManagedCredentialType.registry[namespace] = self

    def get_creation_params(self):
        return dict(
            namespace=self.namespace,
            kind=self.kind,
            name=self.name,
            managed=True,
            inputs=self.inputs,
            injectors=self.injectors,
        )

    def create(self):
        return CredentialType(**self.get_creation_params())


class CredentialInputSource(PrimordialModel):
    class Meta:
        app_label = 'main'
        unique_together = (('target_credential', 'input_field_name'),)
        ordering = (
            'target_credential',
            'source_credential',
            'input_field_name',
        )

    FIELDS_TO_PRESERVE_AT_COPY = ['source_credential', 'metadata', 'input_field_name']

    target_credential = models.ForeignKey(
        'Credential',
        related_name='input_sources',
        on_delete=models.CASCADE,
        null=True,
    )
    source_credential = models.ForeignKey(
        'Credential',
        related_name='target_input_sources',
        on_delete=models.CASCADE,
        null=True,
    )
    input_field_name = models.CharField(
        max_length=1024,
    )
    metadata = DynamicCredentialInputField(blank=True, default=dict)

    def clean_target_credential(self):
        if self.target_credential.credential_type.kind == 'external':
            raise ValidationError(_('Target must be a non-external credential'))
        return self.target_credential

    def clean_source_credential(self):
        if self.source_credential.credential_type.kind != 'external':
            raise ValidationError(_('Source must be an external credential'))
        return self.source_credential

    def clean_input_field_name(self):
        defined_fields = self.target_credential.credential_type.defined_fields
        if self.input_field_name not in defined_fields:
            raise ValidationError(_('Input field must be defined on target credential (options are {}).'.format(', '.join(sorted(defined_fields)))))
        return self.input_field_name

    def get_input_value(self):
        backend = self.source_credential.credential_type.plugin.backend
        backend_kwargs = {}
        for field_name, value in self.source_credential.inputs.items():
            if field_name in self.source_credential.credential_type.secret_fields:
                backend_kwargs[field_name] = decrypt_field(self.source_credential, field_name)
            else:
                backend_kwargs[field_name] = value

        backend_kwargs.update(self.metadata)

        with set_environ(**settings.AWX_TASK_ENV):
            return backend(**backend_kwargs)

    def get_absolute_url(self, request=None):
        view_name = 'api:credential_input_source_detail'
        return reverse(view_name, kwargs={'pk': self.pk}, request=request)


from awx_plugins.credentials.plugins import *  # noqa

for ns, plugin in credential_plugins.items():
    CredentialType.load_plugin(ns, plugin)
