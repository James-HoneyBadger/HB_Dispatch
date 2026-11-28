# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Serverless OAuth2 for IMAP using the RFC 8252 loopback flow.

The user supplies their own desktop OAuth client ID. A short-lived server binds
to 127.0.0.1 on an ephemeral port, the system browser performs the consent, and
the authorization code is exchanged with PKCE (S256). No client secret is used,
because a desktop application cannot keep one secret.

Refresh tokens are stored only in the OS credential store. If no store is
available the flow fails closed rather than writing a token to a file.
"""

import base64
import hashlib
import http.server
import json
import secrets
import threading
import urllib.parse
import urllib.request
import webbrowser

TIMEOUT_SECONDS = 300

PROVIDERS = {
    'google': {
        'authorize': 'https://accounts.google.com/o/oauth2/v2/auth',
        'token': 'https://oauth2.googleapis.com/token',
        'scope': 'https://mail.google.com/',
        'host': 'imap.gmail.com',
    },
    'microsoft': {
        'authorize': 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
        'token': 'https://login.microsoftonline.com/common/oauth2/v2.0/token',
        'scope': 'https://outlook.office.com/IMAP.AccessAsUser.All offline_access',
        'host': 'outlook.office365.com',
        # Entra ID ignores the port only for the "localhost" host name.
        'redirect_host': 'localhost',
    },
}


class OAuthError(RuntimeError):
    """The authorization or token exchange failed."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def pkce_pair() -> tuple[str, str]:
    """Return (verifier, challenge) using S256 as required by RFC 7636."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode('ascii')).digest())
    return verifier, challenge


def provider_config(name: str) -> dict:
    key = (name or '').strip().lower()
    if key == 'gmail':
        key = 'google'
    if key == 'outlook' or key == 'm365' or key == 'office365':
        key = 'microsoft'
    try:
        return dict(PROVIDERS[key], key=key)
    except KeyError:
        raise OAuthError(f'no OAuth endpoints for provider {name!r}; '
                         f'known providers: {", ".join(sorted(PROVIDERS))}') from None


def authorize_url(config: dict, client_id: str, redirect_uri: str, state: str,
                  challenge: str) -> str:
    query = urllib.parse.urlencode({
        'client_id': client_id,
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'scope': config['scope'],
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'access_type': 'offline',
        'prompt': 'consent',
    })
    return f"{config['authorize']}?{query}"


def xoauth2_bytes(user: str, access_token: str) -> bytes:
    """Raw SASL payload for AUTHENTICATE XOAUTH2.

    imaplib.authenticate() base64-encodes whatever the callback returns, so this
    must stay unencoded.
    """
    if not user or not access_token:
        raise OAuthError('user and access token are required')
    return f'user={user}\x01auth=Bearer {access_token}\x01\x01'.encode('utf-8')


def xoauth2_string(user: str, access_token: str) -> str:
    """Base64 SASL payload, for callers that feed the wire themselves."""
    return base64.b64encode(xoauth2_bytes(user, access_token)).decode('ascii')


def _post_form(url: str, fields: dict, opener=None) -> dict:
    opener = opener or urllib.request.urlopen
    data = urllib.parse.urlencode(fields).encode('ascii')
    request = urllib.request.Request(url, data=data, method='POST')
    try:
        with opener(request, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as exc:
        reader = getattr(exc, 'read', None)
        if callable(reader):
            try:
                payload = json.loads(reader().decode('utf-8'))
            except (ValueError, OSError):
                payload = None
            if isinstance(payload, dict):
                raise OAuthError(payload.get('error_description')
                                 or payload.get('error')
                                 or 'token exchange failed') from None
        raise OAuthError(f'token exchange failed: {exc}') from None


def exchange_code(config: dict, client_id: str, code: str, verifier: str,
                  redirect_uri: str, opener=None) -> dict:
    tokens = _post_form(config['token'], {
        'grant_type': 'authorization_code',
        'client_id': client_id,
        'code': code,
        'code_verifier': verifier,
        'redirect_uri': redirect_uri,
    }, opener)
    if 'access_token' not in tokens:
        raise OAuthError('token endpoint returned no access token')
    return tokens


def refresh_access_token(config: dict, client_id: str, refresh_token: str,
                         opener=None) -> dict:
    tokens = _post_form(config['token'], {
        'grant_type': 'refresh_token',
        'client_id': client_id,
        'refresh_token': refresh_token,
        'scope': config['scope'],
    }, opener)
    if 'access_token' not in tokens:
        raise OAuthError('refresh returned no access token')
    return tokens


class _CallbackServer(http.server.HTTPServer):
    expected_state: str = ''
    event: threading.Event
    code: str = ''
    error: str = ''


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server_version = 'HBCallback/1.0'
    timeout = 10

    def setup(self):
        super().setup()
        try:
            self.connection.settimeout(self.timeout)
        except OSError:
            pass

    def log_message(self, *args):  # keep the console quiet
        return

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        server = self.server
        if parsed.path != '/callback':
            self.send_response(404)
            self.end_headers()
            return
        if params.get('state', [''])[0] != server.expected_state:
            server.error = 'state mismatch; refusing the callback'
            body = b'Authorization failed: state mismatch. You can close this window.'
            self.send_response(400)
        else:
            server.code = params.get('code', [''])[0]
            if not server.code:
                server.error = params.get('error', ['unknown error'])[0]
            body = (b'Authorization complete. You can close this window.'
                    if server.code else b'Authorization failed. You can close this window.')
            self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        server.event.set()


class LoopbackServer:
    """A one-shot loopback listener for the OAuth redirect."""

    def __init__(self, state: str, uri_host: str = '127.0.0.1'):
        self.state = state
        self.uri_host = uri_host
        self.event = threading.Event()
        self.code = ''
        self.error = ''
        self.httpd = _CallbackServer(('127.0.0.1', 0), _CallbackHandler)
        self.httpd.expected_state = state
        self.httpd.event = self.event
        self.httpd.code = ''
        self.httpd.error = ''
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def redirect_uri(self) -> str:
        return f'http://{self.uri_host}:{self.port}/callback'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc_info):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False

    def wait(self, timeout: float = TIMEOUT_SECONDS) -> str:
        if not self.event.wait(timeout):
            raise OAuthError('timed out waiting for the browser callback')
        if self.httpd.error:
            raise OAuthError(self.httpd.error)
        return self.httpd.code


def login(provider: str, client_id: str, *, open_browser=True,
          timeout: float = TIMEOUT_SECONDS, opener=None) -> dict:
    """Run the full loopback flow and return the token response."""
    if not client_id.strip():
        raise OAuthError('a desktop OAuth client ID is required')
    config = provider_config(provider)
    verifier, challenge = pkce_pair()
    state = _b64url(secrets.token_bytes(16))
    with LoopbackServer(state, uri_host=config.get('redirect_host', '127.0.0.1')) as server:
        url = authorize_url(config, client_id, server.redirect_uri, state, challenge)
        if open_browser:
            webbrowser.open(url)
        code = server.wait(timeout)
        tokens = exchange_code(config, client_id, code, verifier,
                               server.redirect_uri, opener)
    tokens['provider'] = config['key']
    return tokens


SERVICE_PREFIX = 'HB Continuum IMAP'


class KeyringUnavailable(OAuthError):
    """No OS credential store is available for the refresh token."""


_OS_BACKEND_MODULES = ('keyring.backends.macOS', 'keyring.backends.Windows',
                       'keyring.backends.SecretService', 'keyring.backends.kwallet')


def _keyring():
    try:
        import keyring
    except Exception:
        raise KeyringUnavailable(
            'OAuth refresh tokens are stored only in the OS credential store, and '
            'the "keyring" package is not installed. Install it (Linux also needs '
            'a Secret Service provider such as gnome-keyring) or keep using an '
            'app password.') from None
    try:
        backend = keyring.get_keyring()
    except Exception as exc:
        raise KeyringUnavailable(
            f'the credential store backend could not be selected: {exc}') from None
    module = type(backend).__module__
    if not module.startswith(_OS_BACKEND_MODULES):
        raise KeyringUnavailable(
            'the configured keyring backend is not an operating-system credential '
            f'store ({module}.{type(backend).__name__}). Tokens are never written '
            'to plain files: uninstall the keyrings.alt plaintext backend or '
            'configure a system backend before using OAuth.')
    return keyring


def _account_key(account: str) -> str:
    return f'oauth:{account}'


def store_refresh_token(account: str, refresh_token: str) -> None:
    if not refresh_token:
        raise OAuthError('refusing to store an empty refresh token')
    keyring = _keyring()
    try:
        keyring.set_password(SERVICE_PREFIX, _account_key(account), refresh_token)
    except Exception as exc:
        raise OAuthError(f'the credential store refused the token: {exc}') from None


def load_refresh_token(account: str) -> str:
    keyring = _keyring()
    try:
        token = keyring.get_password(SERVICE_PREFIX, _account_key(account))
    except Exception as exc:
        raise OAuthError(f'the credential store could not be read: {exc}') from None
    if not token:
        raise OAuthError(f'no OAuth token stored for {account}')
    return token


def delete_refresh_token(account: str) -> None:
    keyring = _keyring()
    try:
        keyring.delete_password(SERVICE_PREFIX, _account_key(account))
    except Exception as exc:
        raise OAuthError(f'the credential store could not delete the token: {exc}') from None
