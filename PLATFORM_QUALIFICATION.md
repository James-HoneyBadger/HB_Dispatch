# Platform qualification

What has actually been run, on what hardware, and what has not. Nothing here
is inferred from a passing build on another platform.

## Current status for `v1.0.0`

| Platform | Engine (Python) | Desktop shell | Status |
| --- | --- | --- | --- |
| macOS 14+, Apple Silicon | Qualified | Built and launched | **Qualified** |
| macOS, Intel | Not tested locally | Not built | Unqualified |
| Linux x86_64 | CI test suite only | Not built | Unqualified |
| Windows x86_64 | CI test suite only | Not built | Unqualified |
| Anything else | No | No | Unsupported |

"Qualified" means the artefact was built on that platform, started, exercised
through a representative workflow and left no stray process behind.

## What was verified on macOS Apple Silicon

- 370 Python tests pass on Python 3.12 (`skipped=24` without `sqlcipher3`;
  fewer with it), plus `mypy` clean over the configured file set.
- PyInstaller engine sidecar builds and answers a smoke request.
- Tauri debug build, then a native `.app` bundle containing the ARM64 sidecar.
- The bundle launches, opens an archive, and exits without leaving an
  `hb-engine` process.
- The security vectors in `scripts/security_review_vectors.sh` pass end to end
  with SQLCipher installed.

## What CI proves, and what it does not

CI runs the Python suite on Ubuntu 3.12/3.13, macOS 3.13 and Windows 3.13, plus
scheduler, engine-packaging, type-check, frontend build and CodeQL jobs. That
proves the engine is portable. **It does not prove that a packaged desktop
application works on Linux or Windows**, because no such package is built or
launched.

## Why the desktop matrix is not in CI yet

A Tauri build needs platform toolchains and system libraries (webkit2gtk on
Linux, MSVC and WebView2 on Windows) and takes considerably longer than the
current matrix. Adding it without running it once would create a job that
either fails constantly or is silently non-blocking; neither is honest
evidence. It should be added together with a packaged smoke test, in a change
that can be reviewed on the results.

## How to qualify a new platform

1. Install Python 3.12+, Node 22+, Rust, and the Tauri system prerequisites.
2. `python3 -m unittest discover -s tests -v` and record failures verbatim.
3. `mypy` and `python3 scripts/check_doc_fidelity.py`.
4. `python3 scripts/build_engine.py`, then in `frontend`: `npm ci` and
   `npm run tauri -- build --config src-tauri/tauri.bundle.conf.json`.
5. Launch the bundle, create an archive in a temporary directory, import
   `tests/fixtures/security_review/fictional-2023-03-14.eml`, search for
   `invoice`, verify integrity, then quit.
6. Confirm no `hb-engine` process survives the window closing.
7. `PYTHON=<interpreter with sqlcipher3> ./scripts/security_review_vectors.sh`.
8. Record the results in this file, including anything that failed.

## Known packaging limits

- The macOS bundle is unsigned and not notarized; Gatekeeper will warn.
- `LSMinimumSystemVersion` is generated, not independently verified.
- No installer, no auto-update and no crash reporting exist.

## Known dependency advisory

Dependabot reports `GHSA-wrw7-89jp-8q8g` (unsoundness in `glib` iterator
implementations) against `glib` 0.18.5 in `src-tauri/Cargo.lock`. The patched
release is 0.20.0, which requires the GTK-rs 0.20 chain and therefore a Tauri
dependency upgrade, not a version pin.

`glib` enters the lock file through Tauri's GTK dependencies, which are compiled
only for Linux desktop builds. The qualified macOS Apple Silicon build does not
compile it, and no Linux desktop artifact is shipped or claimed.

An upgrade was attempted and is not possible from this repository:

```
$ cargo update -p glib --precise 0.20.0
error: failed to select a version for the requirement `glib = "^0.18"`
required by package `gtk v0.18.2`
... which satisfies dependency `gtk = "^0.18"` (locked to 0.18.2)
... of package `tauri v2.12.7`
```

`tauri 2.12.7` pins `gtk ^0.18`, so `glib` cannot move to the patched 0.20.0
without an upstream GTK-rs migration. The Dependabot alert is therefore
dismissed as `not_used` with that rationale, not as fixed. Re-check before any
Linux desktop packaging is attempted, and revisit when Tauri adopts GTK-rs 0.20
or later.
