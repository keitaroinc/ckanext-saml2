import logging
import uuid

from saml2 import BINDING_HTTP_REDIRECT

import pylons.config as config

import ckan.plugins as p
import ckan.lib.base as base
import ckan.logic as logic
import ckan.lib.helpers as h
import ckan.model as model
from saml2_model.permissions import AccessPermissions
from access_permission import ACCESS_PERMISSIONS
import ckan.logic.schema as schema
from ckan.controllers.user import UserController
from routes.mapper import SubMapper
from saml2.ident import decode as unserialise_nameid
from saml2.s2repoze.plugins.sp import SAML2Plugin

log = logging.getLogger('ckanext.saml2')
DELETE_USERS_PERMISSION = 'delete_users'


def _no_permissions(context, msg):
    user = context['user']
    return {'success': False, 'msg': msg.format(user=user)}


@logic.auth_sysadmins_check
def user_create(context, data_dict):
    """Deny user creation."""
    msg = p.toolkit._('Users cannot be created.')
    return _no_permissions(context, msg)


@logic.auth_sysadmins_check
def user_update(context, data_dict):
    """Deny user changes."""
    current_user = context['auth_user_obj']

    if isinstance(data_dict, model.User):
        id = data_dict.id
    else:
        id = logic.get_or_bust(data_dict, 'id')
    modified_user = model.User.get(id)

    if is_local_user(modified_user) and (
      current_user.sysadmin or modified_user.id == current_user.id):
            return {'success': True}
    msg = p.toolkit._('Users cannot be edited.')
    return _no_permissions(context, msg)


@logic.auth_sysadmins_check
def user_reset(context, data_dict):
    """Deny user reset."""
    msg = p.toolkit._('Users cannot reset passwords.')
    return _no_permissions(context, msg)


@logic.auth_sysadmins_check
def request_reset(context, data_dict):
    """Deny user reset."""
    msg = p.toolkit._('Users cannot reset passwords.')
    return _no_permissions(context, msg)


def user_delete(context, data_dict):
    """Allow user deletion."""
    # import pprint
    user = context['auth_user_obj']
    msg = p.toolkit._('Users cannot remove users')
    try:
        u_perm = ACCESS_PERMISSIONS.get_user_permissions(user.id)
        if u_perm and u_perm.has_permission(DELETE_USERS_PERMISSION):
            return {'success': True}
    except:
        pass
    # if ACCESS_PERMISSIONS.get_user_permissions()
    return _no_permissions(context, msg)

rememberer_name = None


def delete_cookies():
    """Logout."""
    global rememberer_name
    if rememberer_name is None:
        plugins = p.toolkit.request.environ['repoze.who.plugins']
        saml_plugin = plugins.get('saml2auth')
        rememberer_name = saml_plugin.rememberer_name
    base.response.delete_cookie(rememberer_name)
    # We seem to end up with an extra cookie so kill this too
    domain = p.toolkit.request.environ['HTTP_HOST']
    base.response.delete_cookie(rememberer_name, domain='.' + domain)


def is_local_user(userobj):
    """
    Check whether current user shouldn't use sso and such things.

    :saml2.local_email_domains: - list of space separated domains
    in config file that treated as local
    :saml2.sso_email_domains: - list of space separated domains
    in config file that treated as sso provisioned

    If both are defined and not empty, first one has more precedence.

    Should return (bool)True if user allowed to use native login system and
    anything else, that sso users can't do

    """
    _local_domains = config.get('saml2.local_email_domains', '')
    _sso_domains = config.get('saml2.sso_email_domains', '')

    # precedence defined in next two lines
    is_local_check = True if _local_domains else False
    checked_domains = (_local_domains or _sso_domains).split()

    # there are no any rules for separating users, so let's asuume
    # that all users are created with sso
    if not checked_domains:
        return False

    if userobj:
        email = str(userobj.email)
        return bool(filter(
            lambda d: email.endswith(d), checked_domains)) == is_local_check


@logic.side_effect_free
def access_permission_show(context, data_dict):
    """
    Return access permissions of user.

    :param id: the id or name of the user
    :type id: string
    :rtype: dictionary
    """
    model = context['model']
    context['session'] = model.Session
    id = logic.get_or_bust(data_dict, 'id')

    user = model.User.get(id)
    if user:
        perms = ACCESS_PERMISSIONS.get_user_permissions(user.id)
        if perms:
            return perms.as_dict()


def assign_default_role(context, user_name):
    """Creates organization member roles according to saml2.default_org
    and saml2.default_role or does nothing if those are not set.

    """
    user_org = config.get('saml2.default_org')
    user_role = config.get('saml2.default_role')
    if user_org and user_role:
        member_dict = {
            'id': user_org,
            'username': user_name,
            'role': user_role
        }
        p.toolkit.get_action('organization_member_create')(
            context, member_dict)


class Saml2Plugin(p.SingletonPlugin):
    """SAML2 plugin."""

    p.implements(p.IAuthenticator, inherit=True)
    p.implements(p.IRoutes, inherit=True)
    p.implements(p.IAuthFunctions, inherit=True)
    p.implements(p.IConfigurer, inherit=True)
    p.implements(p.IConfigurable)
    p.implements(p.IActions)

    ACCESS_PERMISSIONS.create_permission(DELETE_USERS_PERMISSION)

    def get_actions(self):
        """Return new api actions."""
        return {
            'access_permission_show': access_permission_show
        }

    def update_config(self, config):
        """Update environment config."""
        p.toolkit.add_resource('fanstatic', 'ckanext-saml2')
        p.toolkit.add_ckan_admin_tab(config, 'manage_permissions', 'Permissions')
        p.toolkit.add_template_directory(config, 'templates')

    def make_mapping(self, key, config):
        """Map user data from .ini file."""
        data = config.get(key)
        mapping = {}
        for item in data.split():
            bits = item.split('~')
            mapping[bits[0]] = bits[1]
        return mapping

    def configure(self, config):
        """Apply mapping."""
        self.user_mapping = self.make_mapping('saml2.user_mapping', config)
        m = self.make_mapping('saml2.organization_mapping', config)
        self.organization_mapping = m

    def before_map(self, map):
        """Add few routes."""
        with SubMapper(
                map, controller='ckanext.saml2.plugin:Saml2Controller') as m:
            m.connect('saml2_unauthorized', '/saml2_unauthorized',
                      action='saml2_unauthorized')
            m.connect('saml2_slo', '/slo', action='slo')
            m.connect('staff_login', '/service/login', action='staff_login')
            m.connect('manage_permissions', '/ckan-admin/manage-permissions',
                      action='manage_permissions', ckan_icon="unlock-alt")
        return map

    def make_password(self):
        """Create a hard to guess password."""
        out = ''
        for n in xrange(8):
            out += str(uuid.uuid4())
        return out

    def identify(self):
        """
        Work around saml2 authorization.

        c.user contains the saml2 id of the logged in user we need to
        convert this to represent the ckan user.
        """
        # Can we find the user?
        c = p.toolkit.c
        environ = p.toolkit.request.environ

        # Don't continue if this user wasn't authenticated by SAML2
        try:
            if not isinstance(environ["repoze.who.identity"]["authenticator"], SAML2Plugin):
                log.debug("User not authenticated by SAML2, giving up")
                return
        except KeyError:
            return
        user = environ.get('REMOTE_USER', None)
        if user is None:
            return

        c.user = unserialise_nameid(user).text
        log.debug("REMOTE_USER = \"{0}\"".format(c.user))
        log.debug("repoze.who.identity = {0}".format(dict(environ["repoze.who.identity"])))

        # get the actual user info from the saml2auth client
        try:
            saml_info = environ["repoze.who.identity"]["user"]
        except KeyError:
            # This is a request in an existing session so no need to provision
            # an account, set c.userobj and return
            c.userobj = model.User.get(c.user)
            return

        try:
            # Update the user account from the authentication response
            # every time
            c.userobj = self._create_or_update_user(c.user, saml_info)
        except Exception as e:
            log.error(
                "Couldn't create or update user account ID:%s", c.user)
            log.error("Error %s", e)
            c.user = None
            return

    def _create_or_update_user(self, user_name, saml_info):
        """Create or update the subject's user account and return the user
        object"""

        is_new_user = False
        userobj = model.User.get(user_name)
        if userobj is None:
            is_new_user = True
        elif userobj.is_deleted():
            # If account exists and is deleted, reactivate it. Assumes
            # only the IAM driving the IdP will deprovision user
            # accounts and wouldn't allow a user to authenticate for
            # this app if they shouldn't have access.
            log.debug("Reactivating user")
            userobj.activate()
            userobj.commit()

        data_dict = {
            'password': self.make_password(),
        }

        # Merge SAML assertions into data_dict according to
        # user_mapping
        self.update_data_dict(data_dict, self.user_mapping, saml_info)

        # Remove validation of the values from id and name fields
        user_schema = schema.default_user_schema()
        user_schema['id'] = [p.toolkit.get_validator('not_empty')]
        user_schema['name'] = [p.toolkit.get_validator('not_empty')]
        context = {'schema': user_schema, 'ignore_auth': True}

        if is_new_user:
            log.debug("Creating user: {0}".format(data_dict))
            p.toolkit.get_action('user_create')(context, data_dict)
            assign_default_role(context, user_name)
        else:
            log.debug("Updating user: {0}".format(data_dict))
            p.toolkit.get_action('user_update')(context, data_dict)

        # previous 'user' in repoze.who.identity check is broken.
        # use referer check as an temp alternative.
        if not p.toolkit.request.environ.get('HTTP_REFERER'):
            if 'name' in self.organization_mapping and self.organization_mapping['name'] in saml_info:
                self.create_organization(saml_info)

        return model.User.get(user_name)

    def create_organization(self, saml_info):
        """Create organization using mapping."""
        org_name = saml_info[self.organization_mapping['name']][0]
        org = model.Group.get(org_name)

        context = {'ignore_auth': True}
        site_user = p.toolkit.get_action('get_site_user')(context, {})
        c = p.toolkit.c

        if not org:
            context = {'user': site_user['name']}
            data_dict = {
            }
            self.update_data_dict(data_dict, self.organization_mapping, saml_info)
            org = p.toolkit.get_action('organization_create')(context, data_dict)
            org = model.Group.get(org_name)

        # check if we are a member of the organization
        data_dict = {
            'id': org.id,
            'type': 'user',
        }
        members = p.toolkit.get_action('member_list')(context, data_dict)
        members = [member[0] for member in members]
        if c.userobj.id not in members:
            # add membership
            member_dict = {
                'id': org.id,
                'object': c.userobj.id,
                'object_type': 'user',
                'capacity': 'editor'
                    if saml_info['field_type_of_user'][0] == 'Publisher'
                    else 'member',
            }
            member_create_context = {
                'user': site_user['name'],
                'ignore_auth': True,
            }

            p.toolkit.get_action('member_create')(member_create_context, member_dict)

    def update_data_dict(self, data_dict, mapping, saml_info):
        """Dumb docstring."""
        for field in mapping:
            value = saml_info.get(mapping[field])
            if value:
                # If list get first value
                if isinstance(value, list):
                    value = value[0]
                if not field.startswith('extras:'):
                    data_dict[field] = value
                else:
                    if 'extras' not in data_dict:
                        data_dict['extras'] = []
                    data_dict['extras'].append(dict(key=field[7:], value=value))

    def login(self):
        """
        Login definition.

        We can be here either because we are requesting a login (no user)
        or we have just been logged in.
        """
        if not p.toolkit.c.user:
            try:
                if p.toolkit.request.environ['pylons.routes_dict']['action'] == 'staff_login':
                    return
            except Exception:
                pass
            return base.abort(401, p.toolkit._('Login required!'))
        h.redirect_to(controller='user', action='dashboard')

    def logout(self):
        """Logout definition."""
        environ = p.toolkit.request.environ

        userobj = p.toolkit.c.userobj
        sp_slo = p.toolkit.asbool(config.get('saml2.sp_initiates_slo', True))
        if not sp_slo or userobj and is_local_user(userobj):
            plugins = environ['repoze.who.plugins']
            friendlyform_plugin = plugins.get('friendlyform')
            rememberer_name = friendlyform_plugin.rememberer_name
            domain = p.toolkit.request.environ['HTTP_HOST']
            base.response.delete_cookie(rememberer_name, domain='.' + domain)
            base.response.delete_cookie(rememberer_name)
            h.redirect_to(controller='home', action='index')

        subject_id = environ["repoze.who.identity"]['repoze.who.userid']
        name_id = unserialise_nameid(subject_id)
        client = environ['repoze.who.plugins']["saml2auth"]

        # Taken from saml2.client:global_logout but forces
        # HTTP-Redirect binding.
        entity_ids = client.saml_client.users.issuers_of_info(name_id)
        saml_logout = client.saml_client.do_logout(name_id, entity_ids,
                                                   reason='urn:oasis:names:tc:SAML:2.0:logout:user',
                                                   expire=None, sign=True,
                                                   expected_binding=BINDING_HTTP_REDIRECT,
                                                   sign_alg="rsa-sha256", digest_alg="hmac-sha256")

        rem = environ['repoze.who.plugins'][client.rememberer_name]
        rem.forget(environ, subject_id)

        # Redirect to send the logout request to the IdP, using the
        # url in saml_logout. Assumes only one IdP will be returned.
        for key in saml_logout.keys():
            location = saml_logout[key][1]['headers'][0][1]
            log.debug("IdP logout URL = {0}".format(location))
            h.redirect_to(location)

    def abort(self, status_code, detail, headers, comment):
        """
        HTTP Status 401 causes a login redirect.

        We need to prevent this unless we are actually trying to login.
        """
        if (status_code == 401 and
           p.toolkit.request.environ['PATH_INFO'] != '/user/login'):
                h.redirect_to('saml2_unauthorized')
        return (status_code, detail, headers, comment)

    def get_auth_functions(self):
        """We need to prevent some actions being authorized."""
        return {
            'user_create': user_create,
            'user_update': user_update,
            'user_reset': user_reset,
            'user_delete': user_delete,
            'request_reset': request_reset,
        }


class Saml2Controller(UserController):
    """SAML2 Controller."""

    _get_repoze_handler = UserController._get_repoze_handler

    def saml2_unauthorized(self):
        """Our you are not authorized page."""
        c = p.toolkit.c
        c.code = 401
        c.content = p.toolkit._('You are not authorized to do this')
        return p.toolkit.render('error_document_template.html')

    def slo(self):
        """SAML magic."""
        environ = p.toolkit.request.environ
        # so here I might get either a LogoutResponse or a LogoutRequest
        client = environ['repoze.who.plugins']['saml2auth']
        if 'QUERY_STRING' in environ:
            saml_resp = p.toolkit.request.GET.get('SAMLResponse', '')
            saml_req = p.toolkit.request.GET.get('SAMLRequest', '')

            if saml_req:
                log.debug('Received SLO request from IdP')
                # Ignore whatever the pysaml2 plugin did, which as of
                # 4.0.0 seems broken, and do it ourselves
                name_id = unserialise_nameid(environ.get('REMOTE_USER'))
                response = client.saml_client.handle_logout_request(
                    saml_req, name_id, BINDING_HTTP_REDIRECT)
                location = client._handle_logout(response).location()
                h.redirect_to(location, code=303)
            elif saml_resp:
             #   # fix the cert so that it is on multiple lines
             #   out = []
             #   # if on multiple lines make it a single one
             #   line = ''.join(saml_resp.split('\n'))
             #   while len(line) > 64:
             #       out.append(line[:64])
             #       line = line[64:]
             #   out.append(line)
             #   saml_resp = '\n'.join(out)
             #   try:
             #       res = client.saml_client.logout_request_response(
             #           saml_resp,
             #           binding=BINDING_HTTP_REDIRECT
             #       )
             #   except KeyError:
             #       # return error reply
             #       pass

                delete_cookies()
                h.redirect_to(controller='user', action='logged_out')

    def staff_login(self):
        """Default login page for staff members."""
        return self.login()

    def manage_permissions(self):
        """Admin page."""
        context = {'model': model,
                   'user': p.toolkit.c.user,
                   'auth_user_obj': p.toolkit.c.userobj}
        try:
            logic.check_access('sysadmin', context, {})
        except logic.NotAuthorized:
            code, msg = 401, 'Not authorized to see this page'
            base.abort(code, p.toolkit._(msg))

        data = p.toolkit.request.POST
        if 'save' in data:
            new_perms = data.getall('perm')
            username = data.get('username')
            user = model.User.get(username)
            if user:
                permissions = ACCESS_PERMISSIONS.get_user_permissions(
                    user.id)
                if not permissions:
                    permissions = AccessPermissions(owner_id=user.id)
                    model.Session.add(permissions)
                permissions.set_permissions(new_perms)
                model.Session.commit()
            return base.redirect(h.full_current_url())

        vars = {'perm_list': ACCESS_PERMISSIONS}
        return base.render('admin/manage_permissions.html',
                           extra_vars=vars)
