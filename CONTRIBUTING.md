# Contributing to HB Continuum

Public, operational FOSS project. Preserve explicit limitations; do not claim verified restoration, certified chain of custody or safe server deletion.

Use synthetic EML and isolated test archives only. Never commit real messages, credentials, exports or mailbox identifiers. Preserve read-only IMAP behavior, original-byte hashing and provenance; distinguish integrity checks from authenticity. Run `python3 -m unittest discover -s tests -v` and explicitly report untested live IMAP, GUI and recovery paths. Include regression tests for malformed input, failure recovery and privacy-sensitive changes. Keep documentation aligned with actual behavior.

Use focused branches and review changes before merging. Respect applicable licenses and provenance; do not assume permission to redistribute private or third-party material. No support timetable is promised; formal releases remain the owner's decision.

`main` is protected by a repository ruleset: changes arrive through a pull request, only squash merges are accepted, history stays linear, force-pushes and branch deletion are refused, and the required CI jobs (`test (ubuntu-latest, 3.12)`, `test (ubuntu-latest, 3.13)`, `typecheck`, `frontend`, `engine (ubuntu-latest)`, `analyze (python)`) must pass. Direct pushes are rejected even for the owner.

## Security reports

Do not open a public issue, pull request or discussion for a suspected
vulnerability. Follow [SECURITY.md](SECURITY.md) and use GitHub's private
vulnerability reporting. Security-sensitive changes should be discussed
privately before any public pull request is opened.

## Security review

If you are reviewing encryption, key handling or credential storage, start with
[SECURITY_REVIEW_CHECKLIST.md](SECURITY_REVIEW_CHECKLIST.md). It lists the exact
files in scope for pass one and a reproducible vector script:

```sh
./scripts/security_review_vectors.sh
PYTHON=/path/to/python-with-sqlcipher3 ./scripts/security_review_vectors.sh
``` Run `mypy` locally with the checked-in `mypy.ini`, and note that `check_untyped_defs` is off while the codebase remains unannotated.

## Development environment

The engine needs only Python 3.12 or newer with SQLite FTS5. The desktop shell
additionally needs Node.js 22+ and Rust.

```sh
# macOS (Homebrew)
brew install python@3.12 node rustup-init
# Debian/Ubuntu
sudo apt install python3.12 nodejs cargo
# Windows: install Python 3.12+ from python.org, Node LTS, and the Rust toolchain
```

Optional packages enable extra capabilities and are never required by the core:

```sh
pip install sqlcipher3 pypdf python-docx keyring cryptography mypy==2.4.1
```

Build the desktop shell with PyInstaller for the engine sidecar:

```sh
pip install pyinstaller
python3 scripts/build_engine.py
cd frontend && npm ci && npm run tauri -- build --config src-tauri/tauri.bundle.conf.json
```

## Running the tests

```sh
python3 -m unittest discover -s tests -v   # full suite
mypy                                       # uses the checked-in mypy.ini
python3 scripts/check_doc_fidelity.py      # release claims vs implementation status
./scripts/quickstart.sh                    # disposable synthetic archive
```

Two variants matter. Without `sqlcipher3` the suite reports `370 tests, OK
(skipped=24)` on Python 3.12; encryption tests skip rather than fail. With
`sqlcipher3` installed the encryption, migration and encrypted-backup tests run
and the skip count drops. Report which variant you ran.

`mypy` must be clean. The configuration deliberately keeps `check_untyped_defs`
off while the codebase remains unannotated; do not add suppressions to silence
new errors.

## Branch strategy

Branch from `main`, keep the change focused, and open a pull request. `main`
accepts squash merges only, requires linear history, and runs the full CI
matrix before merge. Use conventional commit prefixes (`fix:`, `feat:`,
`docs:`, `chore:`, `test:`) and describe what was verified, not what is hoped.

## Coding standards

- Implementations live under `hb_engine/`; root holds only the CLI and Tk entry
  points. Import engine modules by their full path.
- Standard library first. Optional dependencies must be import-guarded and fail
  with an actionable message.
- Fail closed. A missing key, credential backend or integrity check is an error,
  never a silent fallback.
- Every behaviour change ships with a test that fails before the change.
- Never log secrets, at any level.
- Documentation states real capabilities and real limitations.

## What will not be merged

- Telemetry, analytics, crash reporting or update pings.
- Mandatory cloud services, accounts or subscriptions.
- Silent credential fallbacks or silent plaintext fallback for encryption.
- Writes outside the documented encryption boundary that are not disclosed in
  the docstring, user documentation and release notes.

Contributions are accepted under GPL-3.0-only. Submit only material you have the right to license on these terms; retain applicable third-party copyright and license notices.
