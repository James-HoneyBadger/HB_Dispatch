# HB Continuum

[![Security policy](https://img.shields.io/badge/security-policy-informational.svg)](SECURITY.md)

**A local-first personal archive and historical research application.**

HB Continuum is an independent, open-source hobby project developed as part of
the Honey Badger Universe. It helps an individual preserve and explore their
own documented history without depending entirely on external service
providers.

Originally developed to preserve email correspondence, Continuum now also
supports searchable documents, user-obtained social exports, research tools and
optional local artificial intelligence. Development is iterative and driven by
practical use. Features are introduced when they can be implemented, tested and
maintained responsibly.

## Status

**Current release: [v1.0.0](https://github.com/James-HoneyBadger/HB_Continuum/releases/tag/v1.0.0).**
This is a stable source release for personal use, published without a signed
or notarized installer. The native desktop application is qualified on
**macOS Apple Silicon only**. The portable Python engine is exercised by CI on
Linux and Windows, but packaged desktop applications for those platforms are
not claimed as supported.

"Stable" here means the archival core is dependable and the storage formats are
frozen for this series. It does not mean feature-complete: MBOX export,
incremental synchronization, richer research tooling and deeper local
intelligence are planned, not present. The
[roadmap](docs/ROADMAP.md) and
[implementation status](docs/engineering/IMPLEMENTATION_STATUS.md) state what
exists.

## Getting the source

The default branch `main` carries the released implementation, and the
`v1.0.0` tag identifies it:

```sh
git clone https://github.com/James-HoneyBadger/HB_Continuum.git
cd HB_Continuum
python3 dispatch.py --version
```

Security reports go through [SECURITY.md](SECURITY.md); do not open a public
issue for a suspected vulnerability.

## What it does

- Preserves original EML messages under SHA-256 hashes, with IMAP provenance,
  integrity verification and read-only retrieval.
- Imports user-obtained Facebook JSON exports and local documents without
  modifying the originals.
- Provides unified full-text search, timelines, source coverage, collections,
  a research journal and archive reports.
- Supports bounded historical research with source-linked evidence and
  validated citations. Local AI through Ollama is optional and disabled by
  default.
- Supports SQLCipher-encrypted databases for new archives, explicit
  plaintext-to-encrypted migration and verified encrypted backups.
- Keeps the primary workflow local: no telemetry, advertising, subscription or
  mandatory cloud account.

## Use Continuum your way

No AI is required for archival work, searches, collections, journals, reports
or integrity verification. Use the command line for quick lookups, the native
desktop for broader research, or optional local Ollama for evidence-linked
questions. The [practical field guide](docs/USE_CASES.md) compares workflows
with and without AI through eight fictional scenarios, including household
records, disputes, family history, project research and provider migration.

## Requirements

- Python 3.12 or newer with SQLite FTS5 support.
- Node.js 22+ and Rust only when building the Tauri desktop shell.
- Optional local packages for additional capabilities:
  - `sqlcipher3` for encrypted databases;
  - `pypdf` and `python-docx` for PDF and DOCX text extraction;
  - `keyring`/`cryptography` where supported for credential storage.

The core engine uses the Python standard library. Tkinter is only needed for
the legacy Tk workstation.

## Quick start

From a checkout:

```sh
python3 dispatch.py --version
python3 dispatch.py --archive ~/HB_Continuum_Archive import sample.eml
python3 dispatch.py --archive ~/HB_Continuum_Archive search invoice
python3 dispatch.py --archive ~/HB_Continuum_Archive verify
python3 dispatch.py --archive ~/HB_Continuum_Archive health
python3 -m unittest discover -s tests -v
```

`dispatch.py gui` starts the legacy Tk workstation. The native Tauri shell is
the primary desktop application.

Unified imports and search include Facebook exports, documents and all
sources:

```sh
python3 dispatch.py --archive ~/HB_Continuum_Archive mbox preview ~/takeout.mbox
python3 dispatch.py --archive ~/HB_Continuum_Archive mbox import ~/takeout.mbox
python3 dispatch.py --archive ~/HB_Continuum_Archive mbox export ~/exported.mbox
python3 dispatch.py --archive ~/HB_Continuum_Archive facebook import ~/facebook-export
python3 dispatch.py --archive ~/HB_Continuum_Archive documents import ~/documents
python3 dispatch.py --archive ~/HB_Continuum_Archive search-all "receipt"
```

Create a new encrypted archive from the command line:

```sh
python3 dispatch.py --archive ~/EncryptedArchive init-encrypted
```

Check which optional capabilities a given installation actually has:

```sh
python3 dispatch.py capabilities
```

Existing encrypted archives can be opened and unlocked in the desktop
application. Migrate an existing plaintext archive only after reading
[Encrypted archive migration](docs/ENCRYPTION_MIGRATION.md) and verifying an
independent backup.

## Desktop application

On macOS Apple Silicon, build the engine sidecar and the native shell:

```sh
python3 -m pip install pyinstaller
python3 scripts/build_engine.py
cd frontend
npm ci
npm run tauri -- build --config src-tauri/tauri.bundle.conf.json
```

For development:

```sh
cd frontend
npm ci
npm run tauri -- dev
```

The build is unsigned and not notarized. On first launch, choose a folder and
select **Create archive here**, or open an existing archive. See
[Packaging](docs/PACKAGING.md) for the complete layout, local installation
notes and platform limitations.

## Data safety and limitations

Original EML files are preserved as received; hashes detect changes, not
authenticity or a certified chain of custody. HTML is not rendered. Failed
imports can leave recoverable raw files, and failed exports can be partial.

Database encryption protects the SQLite databases only. Raw EML files,
external documents, preferences, exports and configuration remain outside the
database encryption boundary. Encrypted backups keep database files encrypted,
but raw files and configuration in a snapshot remain plaintext. See the
[security model](docs/SECURITY_MODEL.md).

The application cannot prove that a mailbox was fully downloaded. Verify folder
coverage independently. Do not use HB Continuum as the sole copy of
irreplaceable evidence: keep independent backups and test restoration.

Optional local AI can help explore evidence, but model output is derived text,
not preserved evidence. Retrieved records and conversation history are treated
as untrusted data; model output cannot modify the archive. Citation identity is
validated, but factual conclusions and contradiction labels are not
independently proven.

Other current limits include keyword/FTS5 rather than semantic retrieval, no
OCR, no per-token model streaming, a 5,000-entry merged timeline cap, no
signed installer and no packaged-desktop qualification outside macOS Apple
Silicon. Optional PDF/DOCX extraction and credential-store packages are not
guaranteed to be installed.

## Documentation

- [Practical tutorial](docs/TUTORIAL.md)
- [Use cases and object lessons: with and without AI](docs/USE_CASES.md)
- [Packaging and local installation](docs/PACKAGING.md)
- [Encryption and recovery](docs/ENCRYPTION.md)
- [Encrypted archive migration](docs/ENCRYPTION_MIGRATION.md)
- [Security model and threat boundaries](docs/SECURITY_MODEL.md)
- [Intelligence workspace](docs/INTELLIGENCE.md)
- [Documents](docs/DOCUMENTS.md) and [Facebook import](docs/FACEBOOK_IMPORT.md)
- [Scheduled synchronization](docs/SCHEDULED_SYNC.md)
- [Archive hardening](docs/ARCHIVE_HARDENING.md) and [migration](docs/MIGRATION.md)
- [Benchmarks](docs/BENCHMARKS.md) and [implementation status](docs/engineering/IMPLEMENTATION_STATUS.md)
- [Roadmap and known gaps](docs/ROADMAP.md)
- [Architecture and repository layout](docs/ARCHITECTURE.md)
- [IMAP provider presets](docs/IMAP_PROVIDERS.md)
- [Platform qualification](PLATFORM_QUALIFICATION.md) and [security policy](SECURITY.md)

## Development principles

- Reliable preservation and recovery of original information.
- Privacy and protection of sensitive archival records.
- Useful, dependable search and research capabilities.
- Optional, local AI rather than a mandatory service.
- Open formats and user-controlled information.
- Simplicity, maintainability and honest documentation over feature quantity.

Use synthetic test data. Never inspect or modify a personal archive during
development. Use established cryptographic technology; never invent encryption
algorithms or perform unverified production migrations.

Continuum is maintained primarily by one developer as a personal FOSS
undertaking. Development plans are flexible, and there are no commitments
concerning delivery dates, comprehensive platform support or commercial
service levels. Contributions, constructive feedback and independent
development are welcome.

*The intelligence is replaceable. The memories are yours.*

## License

Copyright (c) 2026 Honey Badger Universe.

Current and future distributions of this source tree are licensed under the
[GNU General Public License version 3](LICENSE) (GPL-3.0-only). Commercial use
is permitted under the license, including its source-distribution and copyleft
requirements. Prior distributions released under MIT retain their original
license rights.
