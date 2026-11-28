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
