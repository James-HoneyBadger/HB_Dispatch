# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Known-good IMAP endpoints and honest sign-in guidance.

Presets remove guesswork about host names and ports; they do not bypass a
provider's authentication policy. Where a provider requires an app password or
OAuth, the guidance says so plainly instead of implying that a normal account
password will work.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ImapPreset:
    key: str
    name: str
    host: str
    port: int
    encryption: str
    guidance: str
    aliases: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict:
        return {'key': self.key, 'name': self.name, 'host': self.host,
                'port': self.port, 'encryption': self.encryption,
                'guidance': self.guidance}


PRESETS: tuple[ImapPreset, ...] = (
    ImapPreset(
        key='gmail',
        name='Google Gmail',
        host='imap.gmail.com',
        port=993,
        encryption='TLS',
        guidance='Requires two-step verification, then an app password. Normal '
                 'account passwords are refused. Spaces in the generated '
                 'password are ignored.',
        aliases=('google', 'googlemail'),
    ),
    ImapPreset(
        key='icloud',
        name='Apple iCloud Mail',
        host='imap.mail.me.com',
        port=993,
        encryption='TLS',
        guidance='Requires two-factor authentication and an app-specific '
                 'password created at appleid.apple.com. The Apple ID password '
                 'itself will not work.',
        aliases=('apple', 'me.com'),
    ),
    ImapPreset(
        key='fastmail',
        name='Fastmail',
        host='imap.fastmail.com',
        port=993,
        encryption='TLS',
        guidance='An app password with mail access is required; create one in '
                 'Settings, Privacy and Security. The normal account password is '
                 'rejected for IMAP, and Basic plans do not support IMAP at all.',
        aliases=(),
    ),
    ImapPreset(
        key='ionos',
        name='IONOS',
        host='imap.ionos.com',
        port=993,
        encryption='TLS',
        guidance='Use the mailbox password. IONOS may require the full email '
                 'address as the user name.',
        aliases=('1and1',),
    ),
    ImapPreset(
        key='outlook',
        name='Microsoft Outlook / Microsoft 365',
        host='outlook.office365.com',
        port=993,
        encryption='TLS',
        guidance='OAuth2 is required. Personal Outlook.com accounts no longer '
                 'accept password or app-password IMAP logins, and many '
                 'Microsoft 365 tenants disable basic authentication entirely. '
                 'This engine has no OAuth support yet, so these accounts cannot '
                 'synchronize until it is added.',
        aliases=('microsoft', 'm365', 'office365', 'hotmail', 'live'),
    ),
    ImapPreset(
        key='custom',
        name='Custom IMAP server',
        host='',
        port=993,
        encryption='TLS',
        guidance='Enter the host name from your provider. Use port 993 with '
                 'TLS; port 143 usually means STARTTLS and is not supported.',
        aliases=(),
    ),
)

_BY_KEY = {preset.key: preset for preset in PRESETS}
_ALIASES = {alias: preset.key for preset in PRESETS for alias in preset.aliases}


class UnknownProviderError(ValueError):
    """The requested provider preset does not exist."""


def list_presets() -> list[dict]:
    return [preset.as_dict() for preset in PRESETS]


def resolve(name: str) -> ImapPreset:
    """Look up a preset by key or alias, case-insensitively."""
    key = (name or '').strip().lower()
    if not key:
        raise UnknownProviderError('provider name is required')
    if key in _BY_KEY:
        return _BY_KEY[key]
    if key in _ALIASES:
        return _BY_KEY[_ALIASES[key]]
    known = ', '.join(sorted(_BY_KEY))
    raise UnknownProviderError(f'unknown provider {name!r}; known providers: {known}')
