# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
import base64
import hashlib
import json
import threading
import unittest
import urllib.parse
import urllib.request
from unittest import mock

from hb_engine import oauth_local as oauth


class PKCETests(unittest.TestCase):
    def test_challenge_is_s256_of_the_verifier(self):
        verifier, challenge = oauth.pkce_pair()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode('ascii')).digest()).decode().rstrip('=')
        self.assertEqual(challenge, expected)
        self.assertNotIn('=', challenge)
        self.assertGreaterEqual(len(verifier), 43)

    def test_pairs_are_unique(self):
        self.assertNotEqual(oauth.pkce_pair()[0], oauth.pkce_pair()[0])


class AuthorizeUrlTests(unittest.TestCase):
    def test_google_url_carries_pkce_and_scope(self):
        config = oauth.provider_config('gmail')
        url = oauth.authorize_url(config, 'client-123', 'http://127.0.0.1:9/callback',
                                  'state-abc', 'challenge-xyz')
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertTrue(url.startswith('https://accounts.google.com/'))
        self.assertEqual(query['client_id'], ['client-123'])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertEqual(query['code_challenge'], ['challenge-xyz'])
        self.assertEqual(query['state'], ['state-abc'])
        self.assertIn('mail.google.com', query['scope'][0])

    def test_microsoft_aliases_resolve(self):
        for alias in ('outlook', 'm365', 'office365', 'MICROSOFT'):
            with self.subTest(alias=alias):
                self.assertEqual(oauth.provider_config(alias)['key'], 'microsoft')

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.provider_config('nonexistent')


class XOAuth2Tests(unittest.TestCase):
    def test_payload_matches_the_sasl_format(self):
        encoded = oauth.xoauth2_string('user@example.invalid', 'token-value')
        decoded = base64.b64decode(encoded).decode('utf-8')
        self.assertEqual(decoded, 'user=user@example.invalid\x01auth=Bearer token-value\x01\x01')

    def test_missing_parts_are_rejected(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.xoauth2_string('', 'token')
        with self.assertRaises(oauth.OAuthError):
            oauth.xoauth2_string('user', '')


class TokenEndpointTests(unittest.TestCase):
    def opener(self, payload, status=200):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def read(self): return json.dumps(payload).encode()
        return mock.Mock(return_value=Response())

    def test_exchange_sends_the_verifier_and_returns_tokens(self):
        opener = self.opener({'access_token': 'at', 'refresh_token': 'rt'})
        config = oauth.provider_config('google')
        tokens = oauth.exchange_code(config, 'cid', 'code', 'verifier',
                                     'http://127.0.0.1:9/callback', opener)
        self.assertEqual(tokens['refresh_token'], 'rt')
        request = opener.call_args[0][0]
        body = urllib.parse.parse_qs(request.data.decode())
        self.assertEqual(body['grant_type'], ['authorization_code'])
        self.assertEqual(body['code_verifier'], ['verifier'])
        self.assertNotIn('client_secret', body)

    def test_exchange_without_access_token_fails(self):
        opener = self.opener({'error': 'invalid_grant'})
        with self.assertRaises(oauth.OAuthError):
            oauth.exchange_code(oauth.provider_config('google'), 'cid', 'c', 'v',
                                'http://127.0.0.1:9/callback', opener)

    def test_refresh_uses_the_refresh_grant(self):
        opener = self.opener({'access_token': 'fresh'})
        tokens = oauth.refresh_access_token(oauth.provider_config('microsoft'),
                                            'cid', 'rt', opener)
        self.assertEqual(tokens['access_token'], 'fresh')
        body = urllib.parse.parse_qs(opener.call_args[0][0].data.decode())
        self.assertEqual(body['grant_type'], ['refresh_token'])


class LoopbackTests(unittest.TestCase):
    def test_callback_captures_the_code(self):
        with oauth.LoopbackServer('state-1') as server:
            url = f'{server.redirect_uri}?code=auth-code&state=state-1'
            threading.Thread(target=lambda: urllib.request.urlopen(url, timeout=5),
                             daemon=True).start()
            self.assertEqual(server.wait(5), 'auth-code')

    def test_state_mismatch_is_refused(self):
        with oauth.LoopbackServer('state-1') as server:
            url = f'{server.redirect_uri}?code=auth-code&state=wrong'
            threading.Thread(
                target=lambda: self._swallow(url), daemon=True).start()
            with self.assertRaises(oauth.OAuthError):
                server.wait(5)

    @staticmethod
    def _swallow(url):
        try:
            urllib.request.urlopen(url, timeout=5)
        except Exception:
            pass

    def test_redirect_uri_is_loopback_only(self):
        with oauth.LoopbackServer('s') as server:
            self.assertTrue(server.redirect_uri.startswith('http://127.0.0.1:'))
            self.assertTrue(server.redirect_uri.endswith('/callback'))

    def test_microsoft_uses_the_localhost_uri_form(self):
        # Entra ID ignores the port only for the "localhost" host name.
        with oauth.LoopbackServer('s', uri_host='localhost') as server:
            self.assertTrue(server.redirect_uri.startswith('http://localhost:'))
            self.assertTrue(server.redirect_uri.endswith('/callback'))
        self.assertEqual(oauth.provider_config('outlook')['redirect_host'], 'localhost')


class LoginTests(unittest.TestCase):
    def test_empty_client_id_is_rejected(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.login('google', '   ')

    def test_login_returns_tokens_with_provider(self):
        with mock.patch.object(oauth, 'LoopbackServer') as server_cls, \
             mock.patch.object(oauth, 'exchange_code', return_value={'access_token': 'at'}) as exchange, \
             mock.patch.object(oauth.webbrowser, 'open'):
            server_cls.return_value.__enter__.return_value.redirect_uri = 'http://127.0.0.1:1/callback'
            server_cls.return_value.__enter__.return_value.wait.return_value = 'code'
            tokens = oauth.login('microsoft', 'client-id')
        self.assertEqual(tokens['provider'], 'microsoft')
        self.assertEqual(tokens['access_token'], 'at')
        exchange.assert_called_once()


class _OsBackend:
    pass


_OsBackend.__module__ = 'keyring.backends.macOS'


class KeyringStorageTests(unittest.TestCase):
    def test_a_non_os_backend_is_refused(self):
        class PlaintextFileKeyring:
            pass
        PlaintextFileKeyring.__module__ = 'keyrings.alt.file'
        fake = mock.MagicMock()
        fake.get_keyring.return_value = PlaintextFileKeyring()
        with mock.patch.dict('sys.modules', {'keyring': fake}):
            with self.assertRaises(oauth.KeyringUnavailable) as caught:
                oauth.store_refresh_token('user@example.invalid', 'rt')
        self.assertIn('not an operating-system', str(caught.exception))
        fake.set_password.assert_not_called()

    def test_token_is_stored_under_a_prefixed_account(self):
        fake = mock.MagicMock()
        fake.get_keyring.return_value = _OsBackend()
        with mock.patch.dict('sys.modules', {'keyring': fake}):
            oauth.store_refresh_token('user@example.invalid', 'rt-1')
        fake.set_password.assert_called_once_with(
            oauth.SERVICE_PREFIX, 'oauth:user@example.invalid', 'rt-1')

    def test_missing_keyring_fails_closed_with_advice(self):
        with mock.patch.dict('sys.modules', {'keyring': None}):
            with self.assertRaises(oauth.KeyringUnavailable) as caught:
                oauth.store_refresh_token('user@example.invalid', 'rt')
        self.assertIn('credential store', str(caught.exception))

    def test_absent_token_is_an_error_not_a_fallback(self):
        fake = mock.MagicMock()
        fake.get_keyring.return_value = _OsBackend()
        fake.get_password.return_value = None
        with mock.patch.dict('sys.modules', {'keyring': fake}):
            with self.assertRaises(oauth.OAuthError):
                oauth.load_refresh_token('user@example.invalid')

    def test_empty_token_is_refused(self):
        fake = mock.MagicMock()
        with mock.patch.dict('sys.modules', {'keyring': fake}):
            with self.assertRaises(oauth.OAuthError):
                oauth.store_refresh_token('account', '')
        fake.set_password.assert_not_called()


class XOAuth2SyncTests(unittest.TestCase):
    def test_sync_authenticates_with_xoauth2_instead_of_a_password(self):
        import tempfile
        from pathlib import Path as _Path
        from hb_engine.dispatch import Archive
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(_Path(tmp) / 'archive')
            m = mock.MagicMock(); m.__enter__.return_value = m
            m.select.return_value = ('OK', [b'0'])
            m.response.return_value = ('UIDVALIDITY', [b'1'])
            m.uid.return_value = ('OK', [b''])
            try:
                with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL', return_value=m):
                    archive.sync('host', 'user@example.invalid', '', oauth_token='tok')
            finally:
                archive.db.close()
        m.login.assert_not_called()
        method, callback = m.authenticate.call_args[0]
        self.assertEqual(method, 'XOAUTH2')
        self.assertEqual(callback(None),
                         b'user=user@example.invalid\x01auth=Bearer tok\x01\x01')

    def test_explicit_empty_token_fails_closed_instead_of_logging_in(self):
        import tempfile
        from pathlib import Path as _Path
        from hb_engine.dispatch import Archive
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(_Path(tmp) / 'archive')
            m = mock.MagicMock(); m.__enter__.return_value = m
            try:
                with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL', return_value=m):
                    with self.assertRaises(oauth.OAuthError):
                        archive.sync('host', 'user@example.invalid', 'pw', oauth_token='')
            finally:
                archive.db.close()
        m.login.assert_not_called()
