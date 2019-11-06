# -------------------------------------------------------------------------
#                     The CodeChecker Infrastructure
#   This file is distributed under the University of Illinois Open Source
#   License. See LICENSE.TXT for details.
# -------------------------------------------------------------------------
"""
Thrift client setup and configuration.
"""
from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import getpass
import sys

from thrift.Thrift import TApplicationException

import codechecker_api_shared
from Authentication_v6 import ttypes as AuthTypes

from codechecker_common.logger import get_logger

from codechecker_web.shared.version import CLIENT_API

from . import authentication_helper
from . import product_helper
from . import thrift_helper
from .credential_manager import UserCredentials
from .product import split_product_url

LOG = get_logger('system')


def check_preconfigured_username(username, host, port):
    """
    Checks if username supplied by using preconfigured credentials.
    """
    if not username:
        LOG.error("No username supplied! Please specify the "
                  "username in your "
                  "\"~/.codechecker.passwords.json\" file for "
                  "%s:%d.", host, port)
        sys.exit(1)


def setup_auth_client(protocol, host, port, session_token=None):
    """
    Setup the Thrift authentication client. Returns the client object and the
    session token for the session.
    """
    client = authentication_helper.ThriftAuthHelper(protocol, host, port,
                                                    '/v' + CLIENT_API +
                                                    '/Authentication',
                                                    session_token)

    return client


def handle_auth(protocol, host, port, username, login=False):
    session = UserCredentials()
    auth_token = session.get_token(host, port)
    auth_client = authentication_helper.ThriftAuthHelper(protocol, host,
                                                         port,
                                                         '/v' +
                                                         CLIENT_API +
                                                         '/Authentication',
                                                         auth_token)

    if not login:
        logout_done = auth_client.destroySession()
        if logout_done:
            session.save_token(host, port, None, True)
            LOG.info("Successfully logged out.")
        return

    try:
        handshake = auth_client.getAuthParameters()

        if not handshake.requiresAuthentication:
            LOG.info("This server does not require privileged access.")
            return

        if auth_token and handshake.sessionStillActive:
            LOG.info("You are already logged in.")
            return

    except TApplicationException:
        LOG.info("This server does not support privileged access.")
        return

    methods = auth_client.getAcceptedAuthMethods()
    # Attempt username-password auth first.
    if 'Username:Password' in str(methods):

        # Try to use a previously saved credential from configuration file if
        # autologin is enabled.
        saved_auth = None
        if session.is_autologin_enabled():
            saved_auth = session.get_auth_string(host, port)

        if saved_auth:
            LOG.info("Logging in using preconfigured credentials...")
            username = saved_auth.split(":")[0]
            pwd = saved_auth.split(":")[1]
            check_preconfigured_username(username, host, port)
        else:
            LOG.info("Logging in using credentials from command line...")
            pwd = getpass.getpass(
                "Please provide password for user '{}': ".format(username))

        LOG.debug("Trying to login as %s to %s:%d", username, host, port)
        try:
            session_token = auth_client.performLogin("Username:Password",
                                                     username + ":" +
                                                     pwd)

            session.save_token(host, port, session_token)
            LOG.info("Server reported successful authentication.")
        except codechecker_api_shared.ttypes.RequestFailed as reqfail:
            LOG.error("Authentication failed! Please check your credentials.")
            LOG.error(reqfail.message)
            sys.exit(1)
    else:
        LOG.critical("No authentication methods were reported by the server "
                     "that this client could support.")
        sys.exit(1)


def perform_auth_for_handler(auth_client, host, port, manager):
    # Before actually communicating with the server,
    # we need to check authentication first.

    try:
        auth_response = auth_client.getAuthParameters()
    except TApplicationException:
        auth_response = AuthTypes.HandshakeInformation()
        auth_response.requiresAuthentication = False

    if auth_response.requiresAuthentication and \
            not auth_response.sessionStillActive:

        if manager.is_autologin_enabled():
            auto_auth_string = manager.get_auth_string(host, port)
            if auto_auth_string:
                LOG.info("Logging in using pre-configured credentials...")

                username = auto_auth_string.split(':')[0]
                check_preconfigured_username(username, host, port)

                LOG.debug("Trying to login as '%s' to '%s:%d'", username,
                          host, port)

                # Try to automatically log in with a saved credential
                # if it exists for the server.
                try:
                    session_token = auth_client.performLogin(
                        "Username:Password",
                        auto_auth_string)
                    manager.save_token(host, port, session_token)
                    LOG.info("Authentication successful.")
                    return session_token
                except codechecker_api_shared.ttypes.RequestFailed:
                    pass

        if manager.is_autologin_enabled():
            LOG.error("Invalid pre-configured credentials.")
            LOG.error("Your password has been changed or personal access "
                      "token has been removed which is used by your "
                      "\"~/.codechecker.passwords.json\" file. Please "
                      "remove or change invalid credentials.")
        else:
            LOG.error("Access denied. This server requires "
                      "authentication.")
            LOG.error("Please log in onto the server using 'CodeChecker "
                      "cmd login'.")
        sys.exit(1)


def setup_product_client(protocol, host, port, auth_client=None,
                         product_name=None,
                         session_token=None):
    """Setup the Thrift client for the product management endpoint."""
    cred_manager = UserCredentials()
    session_token = cred_manager.get_token(host, port)

    if not session_token:
        auth_client = setup_auth_client(protocol, host, port)
        session_token = perform_auth_for_handler(auth_client, host, port,
                                                 cred_manager)

    if not product_name:
        # Attach to the server-wide product service.
        product_client = product_helper.ThriftProductHelper(
            protocol, host, port, '/v' + CLIENT_API + '/Products',
            session_token)
    else:
        # Attach to the product service and provide a product name
        # as "viewpoint" from which the product service is called.
        product_client = product_helper.ThriftProductHelper(
            protocol, host, port,
            '/' + product_name + '/v' + CLIENT_API + '/Products',
            session_token)

        # However, in this case, the specified product might not exist,
        # which means we can't communicate with the server orderly.
        if not product_client.getPackageVersion() or \
                not product_client.getCurrentProduct():
            LOG.error("The product '%s' cannot be communicated with. It "
                      "either doesn't exist, or the server's configuration "
                      "is bogus.", product_name)
            sys.exit(1)

    return product_client


def setup_client(product_url):
    """Setup the Thrift Product or Service client and
    check API version and authentication needs.
    """

    try:
        protocol, host, port, product_name = split_product_url(product_url)
    except ValueError:
        LOG.error("Malformed product URL was provided. A valid product URL "
                  "looks like this: 'http://my.server.com:80/ProductName'.")
        sys.exit(2)  # 2 for argument error.

    # Check if local token is available.
    cred_manager = UserCredentials()
    session_token = cred_manager.get_token(host, port)

    # Local token is missing ask remote server.
    if not session_token:
        auth_client = setup_auth_client(protocol, host, port)
        session_token = perform_auth_for_handler(auth_client, host, port,
                                                 cred_manager)

    LOG.debug("Initializing client connecting to %s:%d/%s done.",
              host, port, product_name)

    client = thrift_helper.ThriftClientHelper(
        protocol, host, port,
        '/' + product_name + '/v' + CLIENT_API + '/CodeCheckerService',
        session_token)

    return client
