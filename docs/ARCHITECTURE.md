# Architecture and repository layout

```
dispatch.py            CLI entry point (thin wrapper over hb_engine.dispatch)
desktop.py             Tk workstation entry point (thin wrapper over hb_engine.tk.app)
hb_engine/             the engine: all behaviour lives here
  db.py                  central connection service, SQLCipher pragmas
  dispatch.py            Archive, import, sync, backup, CLI argument parsing
  encryption.py          archive creation, scrypt derivation, unlock and lock
  migration.py           plaintext-to-encrypted migration
  backup.py              snapshot, validation, restore
  catalog.py             source-independent catalog
  documents.py           document ingestion (text, Markdown, CSV, JSON, PDF, DOCX)
  facebook.py            Facebook JSON export importer
  mbox.py                MBOX import, preview and export
  mime_index.py          MIME parsing, attachment extraction, FTS text
  unified.py             unified search across email, catalog and documents
  advanced_search.py     sender/date filters and saved queries
  research.py            workspaces, tags, collections, rules
  reporting.py           reports and collection manifests
  summarize.py           optional loopback Ollama summarisation
  intelligence.py        bounded research conversations with citations
  schedule.py            synchronization profiles and CLI
  schedulers.py          launchd, systemd and Task Scheduler integration
  sync_lock.py           cross-process archive locking
  migrations.py          versioned schema migrations
  ipc.py                 JSON IPC server used by the desktop shell
  search_worker.py       background search for the Tk interface
  tk/                    legacy Tk workstation (app, dialogs, workbench)
frontend/              Vue 3 interface for the Tauri shell
src-tauri/             Rust shell, commands and packaging configuration
scripts/               build, benchmark and qualification helpers
tests/                 unittest suite and synthetic fixtures
docs/                  user and engineering documentation
```

## Conventions

- **One implementation.** Root-level modules are entry points only. Everything
  else lives under `hb_engine/` and is imported by its full path, for example
  `from hb_engine.catalog import Catalog`.
- **Dependency direction.** `hb_engine` never imports from `frontend/`,
  `src-tauri/` or `scripts/`. The desktop shell talks to the engine only
  through `hb_engine/ipc.py`.
- **Optional dependencies are import-guarded** and reported by
  `python3 dispatch.py capabilities`.
- **Standard library first.** The core runs without third-party packages; the
  optional set is declared in `requirements-optional.txt`.

## Why the layout changed

Before 1.0.0 the repository carried eighteen six-line alias modules at the root
(`catalog.py`, `backup.py`, `ipc.py`, and so on) plus four Tk modules. The
aliases existed so older imports kept working after the engine moved into
`hb_engine`. They made the root directory look like the source of the
application when it was only a compatibility surface.

The aliases were removed and the Tk interface moved to `hb_engine/tk/`. Code
inside this repository imports the engine package directly; the root keeps two
entry points so `python3 dispatch.py` and `python3 desktop.py` still work.

If you maintain a script written against the old imports, change

```python
from catalog import Catalog        # before
from hb_engine.catalog import Catalog   # after
```

Nothing else changed: every module kept its name inside the package.
