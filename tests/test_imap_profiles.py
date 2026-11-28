# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
import unittest

from hb_engine.imap_profiles import (PRESETS, UnknownProviderError, list_presets,
                                     resolve)


class PresetTests(unittest.TestCase):
    def test_expected_providers_are_present(self):
        keys = {preset.key for preset in PRESETS}
        self.assertEqual(keys, {'gmail', 'icloud', 'fastmail', 'ionos',
                                'outlook', 'custom'})

    def test_every_preset_uses_tls_on_a_standard_port(self):
        for preset in PRESETS:
            with self.subTest(provider=preset.key):
                self.assertEqual(preset.encryption, 'TLS')
                self.assertEqual(preset.port, 993)
                self.assertTrue(preset.guidance)

    def test_named_providers_carry_a_host(self):
        for key in ('gmail', 'icloud', 'fastmail', 'ionos', 'outlook'):
            with self.subTest(provider=key):
                self.assertTrue(resolve(key).host)
        self.assertEqual(resolve('custom').host, '')

    def test_known_hosts_are_correct(self):
        self.assertEqual(resolve('gmail').host, 'imap.gmail.com')
        self.assertEqual(resolve('icloud').host, 'imap.mail.me.com')
        self.assertEqual(resolve('fastmail').host, 'imap.fastmail.com')
        self.assertEqual(resolve('ionos').host, 'imap.ionos.com')
        self.assertEqual(resolve('outlook').host, 'outlook.office365.com')

    def test_aliases_and_case_are_resolved(self):
        self.assertEqual(resolve('Google').key, 'gmail')
        self.assertEqual(resolve('hotmail').key, 'outlook')
        self.assertEqual(resolve('APPLE').key, 'icloud')
        self.assertEqual(resolve(' 1and1 ').key, 'ionos')

    def test_unknown_provider_lists_alternatives(self):
        with self.assertRaises(UnknownProviderError) as caught:
            resolve('nonexistent.example')
        self.assertIn('gmail', str(caught.exception))

    def test_empty_name_is_rejected(self):
        with self.assertRaises(UnknownProviderError):
            resolve('   ')

    def test_guidance_is_explicit_about_authentication(self):
        self.assertIn('app password', resolve('gmail').guidance.lower())
        self.assertIn('oauth', resolve('outlook').guidance.lower())

    def test_listing_is_json_ready(self):
        rows = list_presets()
        self.assertEqual(len(rows), len(PRESETS))
        self.assertEqual(sorted(rows[0]), ['encryption', 'guidance', 'host',
                                           'key', 'name', 'port'])
