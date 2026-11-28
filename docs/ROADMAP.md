# Roadmap

This is a working list, not a schedule. HB Continuum is maintained by one
developer as a personal FOSS project. Nothing here is promised, dated or
funded, and items may be dropped without notice.

## Where the project stands

`v1.0.0` is the current source release: archival preservation, full-text
search and timelines, document and social-export ingestion, optional local
research through Ollama, SQLCipher database encryption with explicit migration,
and verified backups. The qualification record and remaining limitations are in
[implementation status](engineering/IMPLEMENTATION_STATUS.md) and the
[security model](SECURITY_MODEL.md).

## Near-term candidates

Roughly ordered by practical usefulness. Each is a known gap rather than a
new ambition.

1. **MBOX interoperability.** Import, preview and export now work
   ([tutorial](TUTORIAL.md)); round-trip fidelity is normalised, not byte-identical.
2. **Acquisition breadth.** Incremental synchronization and mailbox discovery
   are implemented; what remains is scheduling several accounts, OAuth for
   providers that reject app passwords, and provider-specific quirks.
3. **Attachment search filters** by name and type, surfaced in the desktop
   search controls.
4. **Recipient-header indexing**, acquisition-time provenance and a duplicate
   report.
5. **OAuth** for providers that no longer accept app passwords.
6. **Streamed model output and cancellation** in the research workspace.
7. **A declared dependency set** covering the optional packages
   (`sqlcipher3`, `pypdf`, `python-docx`) so installation is reproducible.
8. **A signed and notarized macOS build**, if the owner decides to publish
   binaries at all.

Items 1 to 4 were prototyped on the retired
`feat/reliable-sync-and-body-index` branch, which was written against the
pre-rewrite layout. They would need porting into `hb_engine`, not merging.

## Deliberately out of scope

- Telemetry, advertising, subscriptions or a mandatory cloud service.
- Custom cryptographic formats or algorithms.
- Voice interaction, personality simulation, autonomous agents and
  comprehensive knowledge graphs.
- Claims of support for platforms that have not been tested.

## Long-range thinking

A much longer speculative roadmap covering proposed v0.6 to v2.0 themes was
drafted during earlier planning and is **not** adopted policy. It remains on
`develop/continuum-integration` for reference. The project publishes only what
exists and is tested; see the permanent development principles in the
[README](../README.md).

## How work is accepted

One milestone at a time, using synthetic data, with regression tests and an
honest account of what remains unproven. Original records are treated as
irreplaceable, and existing working behaviour is preserved unless a
demonstrated problem requires change.
