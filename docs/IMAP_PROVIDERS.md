# IMAP provider presets

`python3 dispatch.py imap-presets` lists the built-in endpoints. Presets remove
guesswork about host names and ports; they do not change what a provider
requires for authentication.

| Provider | Host | Port | Sign-in requirement |
| --- | --- | --- | --- |
| Google Gmail | `imap.gmail.com` | 993 | Two-step verification plus an app password |
| Apple iCloud | `imap.mail.me.com` | 993 | Two-factor authentication plus an app-specific password |
| Fastmail | `imap.fastmail.com` | 993 | App password recommended |
| IONOS | `imap.ionos.com` | 993 | Mailbox password, usually with the full address as user name |
| Outlook / Microsoft 365 | `outlook.office365.com` | 993 | App password for personal accounts; many tenants disable basic authentication entirely |
| Custom | — | 993 | Host name from your provider |

All presets use implicit TLS on port 993. STARTTLS on port 143 is not supported.

## Why some accounts will still fail

An app password is not a workaround for a provider policy. Microsoft 365
tenants that disable basic authentication reject IMAP password logins outright,
and no preset can change that: OAuth is the only path, and it is not implemented
yet. The same applies to Google accounts where app passwords are blocked by
administrative policy.

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
