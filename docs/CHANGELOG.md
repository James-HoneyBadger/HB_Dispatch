# Changelog

Notable changes per release. Historical detail lives in
[the qualification log](engineering/QUALIFICATION_LOG.md).

## 1.1.0

Acquisition reliability and cross-provider authentication.

- Incremental IMAP synchronization: per-folder UIDVALIDITY and last-seen UID in
  `imap_folder_state` (archive schema 7); a changed UIDVALIDITY resets that
  folder and rescans with SHA-256 deduplication.
- Per-folder coverage in `dispatch.py health`: server count, local count and
  missing count, so incomplete downloads are visible.
- `sync_checkpoints` (archive schema 8): every run recorded before the network
  is touched, closed as `ok`/`error` with the last ingested UID, surviving Ctrl-C
  and crashes.
- OAuth2 loopback PKCE (S256) with `XOAUTH2` IMAP authentication, refresh tokens
  stored only in an OS credential store and file-based keyrings refused.
- IMAP provider presets for Gmail, iCloud, Fastmail, IONOS, Outlook/Microsoft 365
  and Custom, with accurate sign-in guidance.
- MBOX export alongside import and preview, with overwrite protection and a
  plaintext warning.
- Repository layout: root reduced to `dispatch.py` and `desktop.py`; all
  implementations under `hb_engine/`, Tk interface in `hb_engine/tk/`.
- Pre-migration database copies are integrity-checked and hashed before the
  report claims a usable snapshot.
- Fixed: quoted mailbox names mangled by legacy quote-stripping; `--all-folders`
  reporting success when every folder failed; stale folder state after source
  removal; coverage counting earlier UIDVALIDITY epochs; double-base64 SASL
  payload; empty OAuth token falling back to LOGIN; Microsoft redirect host;
  Windows SQLite handle locking in tests.

## Unreleased

- Repository layout: the eighteen root-level alias modules were removed and the
  Tk interface moved to `hb_engine/tk/`. Root now holds only the two entry
  points, `dispatch.py` and `desktop.py`. Import the engine directly
  (`from hb_engine.catalog import Catalog`); see
  [architecture](ARCHITECTURE.md).

## 1.0.0 — stable personal-use release

Storage formats frozen for this series.

- MBOX import and preview, with reconstruction provenance kept distinct from
  original EML and IMAP records. (MBOX export is not included.)
- Security policy, private vulnerability reporting route, and a review
  checklist with reproducible vectors.
- Credential storage fails closed; the encrypted-file fallback requires an
  explicit opt-in and warns about its limits.
- Encryption boundary shown in the application and documented with a verified
  table; the acknowledgement is logged.
- Encryption exercised in CI on Linux and macOS instead of being skipped.
- `dispatch.py capabilities` reports optional dependencies and their versions.
- Dependency declarations split into core, optional, development and packaging
  files.
- Background search worker gained a lifecycle: workers are joined before an
  archive is closed, and the Tk workstation waits briefly for in-flight queries
  when its window closes.
- Documentation claims are machine-checked against the implementation inventory.
- Fixed: the security-review fixture was excluded by `.gitignore`, so fresh
  clones lacked it.
- Fixed: `mypy` failed for developers who had `sqlcipher3` installed.

## 0.5.0-beta

- Practical historical research: bounded retrieval, validated citations,
  follow-up conversations and missing-evidence reporting.
- Document and attachment ingestion (text, Markdown, CSV, JSON, PDF, DOCX) and
  merged email/document timelines.
- Unified search across email, catalog and documents.
- SQLCipher encrypted archive creation, unlock and lock, encrypted backup and
  restore, and explicit plaintext-to-encrypted migration.
- Everyday desktop usability: first-run archive discovery and creation, native
  macOS menus and folder chooser, recursive EML import, improved error text.

## 0.4.0-beta

- Source-independent catalog, Facebook JSON import, catalog health checks and
  catalog backup coverage.
- Tauri 2 and Vue 3 desktop shell alongside the Tk workstation.

## 0.3.0

- Original EML preservation with SHA-256 addressing, FTS5 search, verification,
  evidence export and scheduled synchronization.
