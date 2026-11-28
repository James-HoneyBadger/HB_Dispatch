# IMAP provider presets

`python3 dispatch.py imap-presets` lists the built-in endpoints. Presets remove
guesswork about host names and ports; they do not change what a provider
requires for authentication.

| Provider | Host | Port | Sign-in requirement |
| --- | --- | --- | --- |
| Google Gmail | `imap.gmail.com` | 993 | Two-step verification plus an app password |
| Apple iCloud | `imap.mail.me.com` | 993 | Two-factor authentication plus an app-specific password |
| Fastmail | `imap.fastmail.com` | 993 | App password with mail access required; Basic plans have no IMAP |
| IONOS | `imap.ionos.com` | 993 | Mailbox password, usually with the full address as user name |
| Outlook / Microsoft 365 | `outlook.office365.com` | 993 | OAuth2 required; password and app-password logins are refused |
| Custom | — | 993 | Host name from your provider |

All presets use implicit TLS on port 993. STARTTLS on port 143 is not supported.

## Why some accounts will still fail

An app password is not a workaround for a provider policy. **Outlook.com
personal accounts and Microsoft 365 tenants no longer accept password or
app-password IMAP logins**; OAuth2 is required, and this engine has no OAuth
support yet, so those accounts cannot synchronize until it is added. Fastmail
requires an app password with mail access and its Basic plan has no IMAP at all.
Google accounts where app passwords are blocked by administrative policy are in
the same position.

When the guidance column says OAuth is required, treat a password prompt as a
provider decision rather than a Continuum defect.

## Using a preset with synchronization

```sh
python3 dispatch.py imap-presets              # list everything as JSON
python3 dispatch.py --archive ~/Archive sync HOST USER --folder INBOX
```

`sync` takes the host directly, so copy it from the preset table. The presets
exist as a machine-readable reference for the desktop wizard that is still to be
built, and as documentation that the sign-in guidance is accurate.


## OAuth2 for providers that refuse passwords

Microsoft (personal and most Microsoft 365 tenants) and some Google accounts
cannot authenticate over IMAP with a password at all. Continuum can run the
standard loopback OAuth flow with your own desktop client ID:

```sh
python3 dispatch.py oauth-login microsoft you@example.com --client-id <client-id>
python3 dispatch.py oauth-login google    you@example.com --client-id <client-id>
```

How it works: a short-lived server binds to `127.0.0.1` on an ephemeral port,
your browser performs the consent, and the authorization code is exchanged with
PKCE (S256). No client secret is used, because a desktop application cannot keep
one. The refresh token is written **only** to the OS credential store; if the
`keyring` package is missing the command fails with an explanation instead of
saving a token somewhere else.

Registration hints: for Google use a "Desktop app" OAuth client; for Microsoft
use a public/native client with `http://127.0.0.1` as a loopback redirect. Then
pass the access token to synchronization through the engine API
(`Archive.sync(..., oauth_token=...)`), which authenticates with `XOAUTH2`
rather than `LOGIN`.

Not yet wired: refreshing an expired access token automatically inside `sync`,
and a desktop UI for the flow. Both are tracked as unfinished work.
