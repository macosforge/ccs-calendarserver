##
# Copyright (c) 2001-2004 Twisted Matrix Laboratories.
# Copyright (c) 2006-2017 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##

"""
Kerberos authentication module.

This implements two authentication modes:

    1. An alternative to password based BASIC authentication in which the BASIC credentials are
        verified against Kerberos.

    2. The NEGOTIATE mechanism (as defined in http://www.ietf.org/rfc/rfc4559.txt)
        that implements full GSSAPI authentication.
"""

__all__ = [
    "BasicKerberosCredentials",
    "BasicKerberosCredentialFactory",
    "BasicKerberosCredentialsChecker",
    "NegotiateCredentials",
    "NegotiateCredentialFactory",
    "NegotiateCredentialsChecker",
]

from zope.interface import implements

import kerberos

from twisted.cred import checkers, credentials, error
from twisted.internet.defer import succeed
from txweb2 import responsecode
from txweb2.auth.interfaces import ICredentialFactory
from txweb2.dav.auth import IPrincipalCredentials

from twext.python.log import Logger


class KerberosCredentialFactoryBase(object):
    """
    Code common to Kerberos-based credential factories.
    """
    log = Logger()

    implements(ICredentialFactory)

    def __init__(self, principal=None, serviceType=None, hostname=None):
        """

        @param principal:  full Kerberos principal (e.g., 'HTTP/server.example.com@EXAMPLE.COM'). If C{None}
            then the type and hostname arguments are used instead.
        @type principal:     str
        @param serviceType:       service type for Kerberos (e.g., 'HTTP'). Must be C{None} if principal used.
        @type serviceType:        str
        @param hostname:   hostname for this server. Must be C{None} if principal used.
        @type hostname:    str
        """

        # Only certain combinations of arguments allowed
        assert (principal and not serviceType and not hostname) or (not principal and serviceType and hostname)

        if not principal:
            # Look up the Kerberos principal given the service type and hostname, and extract
            # the realm and a service principal value for later use.
            try:
                principal = kerberos.getServerPrincipalDetails(serviceType, hostname)
            except kerberos.KrbError as ex:
                self.log.error("getServerPrincipalDetails: {ex}", ex=ex[0])
                raise ValueError('Authentication System Failure: %s' % (ex[0],))

        self.service, self.realm = self._splitPrincipal(principal)

    def _splitPrincipal(self, principal):

        try:
            splits = principal.split("/")
            servicetype = splits[0]
            splits = splits[1].split("@")
            service = splits[0].upper()
            realm = splits[1]
        except IndexError:
            self.log.error("Invalid Kerberos principal: {principal}", principal=principal)
            raise ValueError('Authentication System Failure: Invalid Kerberos principal: %s' % (principal,))

        service = "%s@%s" % (servicetype, service,)
        realm = realm

        return (service, realm,)


class BasicKerberosCredentials(credentials.UsernamePassword):
    """
    A set of user/password credentials that checks itself against Kerberos.
    """

    def __init__(self, username, password, service, realm):
        """

        @param username:   user name of user to authenticate
        @type username:    str
        @param password:   password for user being authenticated
        @type password:    str
        @param service:    service principal
        @type service:     str
        @param hostname:   realm
        @type hostname:    str
        """
        credentials.UsernamePassword.__init__(self, username, password)

        # Convert Kerberos principal spec into service and realm
        self.service = service
        self.default_realm = realm


class BasicKerberosCredentialFactory(KerberosCredentialFactoryBase):
    """
    Authorizer for insecure Basic (base64-encoded plaintext) authentication.

    This form of authentication is insecure and should only be used when SSL is in effect.
    Right now we do not check for that.
    """

    scheme = 'basic'

    def __init__(self, principal=None, serviceType=None, hostname=None):
        """

        @param principal:  full Kerberos principal (e.g., 'HTTP/server.example.com@EXAMPLE.COM'). If C{None}
            then the type and hostname arguments are used instead.
        @type principal:     str
        @param serviceType:       service type for Kerberos (e.g., 'HTTP'). Must be C{None} if principal used.
        @type serviceType:        str
        @param hostname:   hostname for this server. Must be C{None} if principal used.
        @type hostname:    str
        """

        super(BasicKerberosCredentialFactory, self).__init__(principal, serviceType, hostname)

    def getChallenge(self, _ignore_peer):
        return succeed({'realm': self.realm})

    def decode(self, response, request):  # @UnusedVariable
        try:
            creds = (response + '===').decode('base64')
        except:
            raise error.LoginFailed('Invalid credentials')

        creds = creds.split(':', 1)
        if len(creds) == 2:
            c = BasicKerberosCredentials(creds[0], creds[1], self.service, self.realm)
            return succeed(c)
        raise error.LoginFailed('Invalid credentials')


class BasicKerberosCredentialsChecker(object):
    log = Logger()

    implements(checkers.ICredentialsChecker)

    credentialInterfaces = (IPrincipalCredentials,)

    def requestAvatarId(self, credentials):

        # If there is no calendar principal URI then the calendar user is disabled.
        pcreds = IPrincipalCredentials(credentials)

        creds = pcreds.credentials
        if isinstance(creds, BasicKerberosCredentials):
            try:
                kerberos.checkPassword(creds.username, creds.password, creds.service, creds.default_realm)
            except kerberos.BasicAuthError as ex:
                self.log.error("{ex}", ex=ex[0])
                raise error.UnauthorizedLogin("Bad credentials for: %s (%s: %s)" % (pcreds.authnURI, ex[0], ex[1],))
            else:
                return succeed((
                    pcreds.authnPrincipal,
                    pcreds.authzPrincipal,
                ))

        raise error.UnauthorizedLogin("Bad credentials for: %s" % (pcreds.authnURI,))


class NegotiateCredentials(object):
    """
    A set of user/password credentials that checks itself against Kerberos.
    """

    implements(credentials.ICredentials)

    def __init__(self, principal, username):

        self.principal = principal
        self.username = username


class NegotiateCredentialFactory(KerberosCredentialFactoryBase):
    """
    Authorizer for Negotiate authentication (http://www.ietf.org/rfc/rfc4559.txt).
    """

    scheme = 'negotiate'

    def __init__(self, principal=None, serviceType=None, hostname=None):
        """

        @param principal:  full Kerberos principal (e.g., 'HTTP/server.example.com@EXAMPLE.COM'). If C{None}
            then the type and hostname arguments are used instead.
        @type principal:     str
        @param serviceType:       service type for Kerberos (e.g., 'HTTP'). Must be C{None} if principal used.
        @type serviceType:        str
        @param hostname:   hostname for this server. Must be C{None} if principal used.
        @type hostname:    str
        """

        super(NegotiateCredentialFactory, self).__init__(principal, serviceType, hostname)

    def getChallenge(self, _ignore_peer):
        return succeed({})

    def decode(self, base64data, request):

        # Init GSSAPI first - we won't specify the service now as we need to accept a target
        # name that is case-insenstive as some clients will use "http" instead of "HTTP"
        try:
            _ignore_result, context = kerberos.authGSSServerInit("")
        except kerberos.GSSError as ex:
            self.log.error("authGSSServerInit: {ex0}({ex1})", ex0=ex[0][0], ex1=ex[1][0])
            raise error.LoginFailed('Authentication System Failure: %s(%s)' % (ex[0][0], ex[1][0],))

        # Do the GSSAPI step and get response and username
        try:
            kerberos.authGSSServerStep(context, base64data)
        except kerberos.GSSError as ex:
            self.log.error("authGSSServerStep: {ex0}(ex1)", ex0=ex[0][0], ex1=ex[1][0])
            kerberos.authGSSServerClean(context)
            raise error.UnauthorizedLogin('Bad credentials: %s(%s)' % (ex[0][0], ex[1][0],))
        except kerberos.KrbError as ex:
            self.log.error("authGSSServerStep: {ex}", ex=ex[0])
            kerberos.authGSSServerClean(context)
            raise error.UnauthorizedLogin('Bad credentials: %s' % (ex[0],))

        targetname = kerberos.authGSSServerTargetName(context)
        try:
            service, _ignore_realm = self._splitPrincipal(targetname)
        except ValueError:
            self.log.error("authGSSServerTargetName invalid target name: '{target}'", target=targetname)
            kerberos.authGSSServerClean(context)
            raise error.UnauthorizedLogin('Bad credentials: bad target name %s' % (targetname,))
        if service.lower() != self.service.lower():
            self.log.error("authGSSServerTargetName mismatch got: '{got}' wanted: '{wanted}'", got=service, wanted=self.service)
            kerberos.authGSSServerClean(context)
            raise error.UnauthorizedLogin('Bad credentials: wrong target name %s' % (targetname,))

        response = kerberos.authGSSServerResponse(context)
        principal = kerberos.authGSSServerUserName(context)
        username = principal
        realmname = ""

        # Username may include realm suffix which we want to strip
        if username.find("@") != -1:
            splits = username.split("@", 1)
            username = splits[0]
            realmname = splits[1]

        # We currently do not support cross-realm authentication, so we
        # must verify that the realm we got exactly matches the one we expect.
        if realmname != self.realm:
            username = principal

        # Close the context
        try:
            kerberos.authGSSServerClean(context)
        except kerberos.GSSError as ex:
            self.log.error("authGSSServerClean: {ex0}({ex1})", ex0=ex[0][0], ex1=ex[1][0])
            raise error.LoginFailed('Authentication System Failure %s(%s)' % (ex[0][0], ex[1][0],))

        # If we successfully decoded and verified the Kerberos credentials we need to add the Kerberos
        # response data to the outgoing request

        wwwauth = '%s %s' % (self.scheme, response)

        def responseFilterAddWWWAuthenticate(request, response):  # @UnusedVariable
            if response.code != responsecode.UNAUTHORIZED:
                response.headers.addRawHeader('www-authenticate', wwwauth)
            return response

        responseFilterAddWWWAuthenticate.handleErrors = True

        request.addResponseFilter(responseFilterAddWWWAuthenticate)

        return succeed(NegotiateCredentials(principal, username))


class NegotiateCredentialsChecker(object):

    implements(checkers.ICredentialsChecker)

    credentialInterfaces = (IPrincipalCredentials,)

    def requestAvatarId(self, credentials):
        # NB If we get here authentication has already succeeded as it is done in NegotiateCredentialsFactory.decode
        # So all we need to do is return the principal URIs from the credentials.

        # Look for proper credential type.
        pcreds = IPrincipalCredentials(credentials)

        creds = pcreds.credentials
        if isinstance(creds, NegotiateCredentials):
            return succeed((
                pcreds.authnPrincipal,
                pcreds.authzPrincipal,
            ))

        raise error.UnauthorizedLogin("Bad credentials for: %s" % (pcreds.authnURI,))
