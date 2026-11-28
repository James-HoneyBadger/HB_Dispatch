# Implementation status — HB Continuum

This file is the single source of truth for what exists in the released source.
It is verified against the `v1.0.0` tag and is checked in CI by
`scripts/check_doc_fidelity.py`, which fails when a release note claims a
capability that is not recorded here.

Exactly three states are used:

- **Implemented** — code and automated tests are in the tagged source.
- **Experimental (flag-gated)** — code is in the tagged source but depends on an
  optional package, an explicit setting, or has no automated coverage. The gate
  is named.
- **Not yet committed** — not present in the tagged source. These are proposals,
  not capabilities.

The historical qualification record, including earlier test runs and review
notes, is kept in [QUALIFICATION_LOG.md](QUALIFICATION_LOG.md). It describes how
the release was qualified, not what it contains.

## Inventory

| Feature | Key | State | Evidence |
| --- | --- | --- | --- |
| Original EML preservation, SHA-256 addressing, deduplication, IMAP provenance, FTS5 index | `email-preservation` | Implemented | `tests/test_dispatch.py` |
| Read-only IMAP ingestion | `imap-sync` | Implemented | `tests/test_dispatch.py` |
| Integrity verification and evidence export | `evidence-verification` | Implemented | `tests/test_dispatch.py` |
| Advanced search: sender/date filters, saved queries | `advanced-search` | Implemented | `tests/test_advanced_search.py`, `tests/test_saved_queries.py` |
| Research overlay: workspaces, tags, collections, journal, relationships, reports | `research-overlay` | Implemented | `tests/test_research.py`, `tests/test_collections.py`, `tests/test_journal.py`, `tests/test_relationships.py`, `tests/test_reporting.py` |
| Catalog database and Facebook JSON ingestion with verification | `catalog-facebook` | Implemented | `tests/test_facebook.py`, `tests/test_sources.py` |
| Document ingestion (text, Markdown, CSV, JSON, with optional PDF/DOCX) | `document-ingestion` | Implemented | `tests/test_documents.py` |
| Bounded MIME extraction and attachment text indexing | `mime-attachments` | Implemented | `tests/test_hardening.py` |
| Unified search across email, catalog and documents (`search-all`) | `unified-search` | Implemented | `tests/test_unified.py` |
| Bounded research conversations with validated citations | `research-conversations` | Implemented | `tests/test_intelligence.py` |
| Merged email and document timeline | `document-timeline` | Implemented | `tests/test_timeline.py` |
| Loopback Ollama summarisation with prompt versioning | `local-ai-summaries` | Implemented | `tests/test_summarize.py` |
| Desktop IPC engine over stdio (`dispatch serve`) | `desktop-ipc-engine` | Implemented | `tests/test_ipc.py` |
| Background search worker with cancellation | `background-search` | Implemented | `tests/test_search_worker.py` |
| Portable scheduler: launchd, systemd user timers, Task Scheduler | `portable-scheduler` | Implemented | `tests/test_schedulers.py`, `tests/test_schedule.py` |
| Verified backup, verification and restore | `backup-restore` | Implemented | `tests/test_hardening.py` |
| Versioned transactional schema migrations | `schema-migrations` | Implemented | `tests/test_migrations.py` |
| Encrypted archive creation (SQLCipher, scrypt derivation, descriptor file) | `encrypted-creation` | Implemented | `tests/test_encryption.py` |
| Archive unlock and explicit lock | `unlock-lock` | Implemented | `tests/test_encryption.py` |
| Plaintext-to-encrypted migration to a separate destination | `encrypted-migration` | Implemented | `tests/test_migration.py` |
| Encrypted backup and restore without plaintext copies | `encrypted-backup` | Implemented | `tests/test_encryption.py` |
| Cross-process archive locking with stale-lock recovery | `archive-locking` | Implemented | `tests/test_concurrency.py` |
| First-run archive discovery and creation over IPC | `first-run-archive` | Implemented | `tests/test_ipc.py` |
| Engine sidecar packaging check | `engine-packaging` | Implemented | `tests/test_build_engine.py` |
| Encryption boundary notice in the desktop with logged acknowledgement | `boundary-notice` | Implemented | `tests/test_ipc.py` |
| Credential fallback refused unless explicitly opted into | `credential-fail-closed` | Implemented | `tests/test_schedulers.py` |
| Legacy Tk workstation | `legacy-tk-workstation` | Experimental (flag-gated) | Separate entry point `dispatch.py gui`; no automated UI coverage |
| PDF and DOCX text extraction | `pdf-docx-extraction` | Experimental (flag-gated) | Requires optional `pypdf` / `python-docx`; `tests/test_documents.py` skips without them |
| OS credential store for scheduler passwords | `keyring-credentials` | Experimental (flag-gated) | Requires optional `keyring`; `tests/test_schedulers.py`; otherwise an encrypted-file store is used |
| Live model output from Ollama | `live-ollama` | Experimental (flag-gated) | Requires `ai.enabled` and a loopback server; not exercised in CI |
| MBOX import with reconstruction provenance | `mbox-import` | Implemented | `tests/test_mbox.py` |
| MBOX preview: counts, date range, duplicates, problems | `mbox-preview` | Implemented | `tests/test_mbox.py` |
| MBOX export with manifest and overwrite protection | `mbox-export` | Implemented | `tests/test_mbox.py` |
| Incremental IMAP synchronization via a UID watermark | `incremental-sync` | Implemented | `tests/test_dispatch.py` |
| Mailbox discovery and multi-folder runs | `mailbox-discovery` | Implemented | `tests/test_dispatch.py` |
| Attachment-name and attachment-type search filters | `attachment-filters` | Not yet committed | No code in the tag |
| Recipient-header indexing and duplicate report | `recipient-index` | Not yet committed | No code in the tag |
| OAuth mail authentication | `oauth` | Not yet committed | No code in the tag |
| Semantic or hybrid retrieval | `semantic-retrieval` | Not yet committed | No code in the tag |
| Streamed model output | `model-streaming` | Not yet committed | No code in the tag |
| OCR for images and scanned documents | `ocr` | Not yet committed | No code in the tag |
| Signed or notarized installer | `signed-installer` | Not yet committed | No code in the tag |
| Packaged desktop qualification outside macOS Apple Silicon | `cross-platform-desktop` | Not yet committed | No packaged build has been qualified |

## Notes on specific states

- `encrypted-creation` requires the optional `sqlcipher3` package. Without it the
  command fails with an explicit error; it does not fall back to plaintext.
- `encrypted-migration` never deletes the original archive and never migrates
  automatically.
- `keyring-credentials` prefers the OS store. Where no backend is available the
  scheduler refuses to store a password unless
  `--allow-insecure-credential-fallback` is passed; the trade-off is recorded in
  [SECURITY.md](../../SECURITY.md).
- `document-ingestion` indexes what the optional extractors can read. Image-only
  PDFs produce no text (`ocr` above).

## Encryption boundary

Database encryption covers the SQLite databases only. Raw EML files, external
documents, configuration, exports and logs remain outside it. The full boundary
table is in [the security model](../SECURITY_MODEL.md).

## Updating this file

1. Change the code and add tests.
2. Change the row's state and evidence in the table above.
3. If a release note claims the capability, the note must carry a matching
   `<!-- claim: key -->` marker, or CI fails.
