# HB Continuum in practice — a field guide

Continuum is a personal records archive first, a research workstation second,
and an optional local AI assistant third. No Ollama installation, AI account
or subscription is required to preserve, search, organize or verify records.
All scenarios below are fictional object lessons, grounded in the
[v1.0.0 implementation inventory](engineering/IMPLEMENTATION_STATUS.md).

## Choose your working style

| Task | Without AI | With optional local AI |
| --- | --- | --- |
| Find a known message | Keyword and field search | Usually unnecessary |
| Preserve correspondence | EML import, read-only IMAP, MBOX import | No role |
| Explore multiple sources | Unified search, filters, timeline | Focused questions over retrieved evidence |
| Reconstruct a history | Collections, tags, journal, reports | Draft a chronology for human review |
| Compare accounts | Inspect originals and record disagreements | Suggest possible disagreements to investigate |
| Verify and recover | Hash verification, backup and restore | No role |

Start without AI. Enable it when comparing a bounded set of records would
benefit from another analytical pass. Return to originals before deciding.

## Getting started

Run commands from the source checkout, not the home directory. Substitute
your existing archive path, or use a disposable practice archive.

```sh
cd ~/HB_Continuum
python3 dispatch.py capabilities
python3 dispatch.py --archive ~/continuum-practice search invoice
python3 dispatch.py --archive ~/continuum-practice search-all 'renewal OR cancellation'
python3 dispatch.py --archive ~/continuum-practice gui
```

The last command launches legacy Tk, not the full native workstation. Use
the native desktop application for the broader research workflow; follow
[Packaging](PACKAGING.md). macOS Apple Silicon is the qualified native
desktop platform; the Python engine is portable. The [tutorial](TUTORIAL.md)
provides a synthetic message to import first. Empty results in a new archive
are normal.

## 1. Household billing dispute

**Situation:** A fictional utility bills a household after cancellation.
Records include a cancellation confirmation, invoice and later payment demand.

**Without AI:** Import relevant emails and documents. Search the invoice
identifier, company name and phrases such as "service ended". Apply sender
and date filters where available; inspect originals, create a collection
and record a dated chronology in the journal. Export selected original EML
records and their manifest if needed.

**With AI:** Restrict Intelligence to the collection and relevant date range.
Ask: "What cancellation date is documented, what charges were subsequently
requested, and which records disagree? Cite each event and identify missing
evidence." Inspect each cited original and correct the chronology yourself.

**Outcome and lesson:** A traceable correspondence packet, not a legal
finding. A payment demand does not prove that a debt is owed. AI cannot
authenticate claims, determine liability or guarantee admissibility.

## 2. Warranty and household maintenance

**Situation:** A washing machine fails two years after purchase. The receipt,
installation correspondence and warranty terms are in different accounts.

**Without AI:** Import the receipt and warranty, search the model number
and retailer, compare dates and save relevant records in a collection.

**With AI:** Ask: "Which retrieved records document purchase, installation,
warranty duration and exclusions? What is missing?" Verify every proposed
date against the actual terms.

**Outcome and lesson:** A useful packet for a service call. A precise lookup
often needs no AI; comparison across documents may benefit from it.
Continuum does not determine coverage or submit claims.

## 3. Family history and personal memory

**Situation:** A family wants to reconstruct a move or reunion from old emails,
a user-obtained Facebook export and personal notes.

**Without AI:** Import supported exports and documents. Search names, places
and distinctive phrases. Browse the merged timeline and journal confirmed
events and unresolved questions. Keep separate archives when appropriate.

**With AI:** Ask: "What records describe the 2019 reunion, and which dates
are directly documented rather than inferred?" Check dates against originals;
distinguish the message date from the event date.

**Outcome and lesson:** A source-linked draft history that a family member
can curate. A record of someone's life is not a complete account of that life.
The archive supports recall without pretending to replace memory.

## 4. Independent researcher or local historian

**Situation:** A volunteer researches a neighborhood garden through newsletters,
minutes, correspondence and public documents.

**Without AI:** Import authorized material, search names and alternate spellings,
organize by topic, check source coverage and note gaps. Use the journal to
distinguish primary records from retrospective accounts.

**With AI:** Ask: "Compare the retrieved accounts of the garden's opening.
Which records support each date, and where do they disagree?" Follow citations
to originals and run alternate keyword searches.

**Outcome and lesson:** An inspectable working bibliography and timeline,
not a definitive automatically generated history. Absence of a search match
is not proof an event never happened.

## 5. Sole proprietor or volunteer treasurer

**Situation:** An association needs invoice approvals and correspondence
before handing records to its next treasurer.

**Without AI:** Archive permitted mail folders and invoices. Search supplier
names and invoice identifiers; organize by project or year. Use reports and
selected EML exports as supporting materials.

**With AI:** Ask: "Which retrieved records describe approval of invoice 1042?
Separate approval, payment claims and unresolved questions." Compare against
the actual accounting ledger and bank records.

**Outcome and lesson:** A supporting handover package. Continuum is not an
accounting system, reconciliation engine, tax preparer or audit opinion.
Archives complement authoritative operational systems.

## 6. Open-source project history

**Situation:** A maintainer wants to know why a design choice was made
eighteen months earlier.

**Without AI:** Import selected Markdown notes, decision records and email.
Search the subsystem and terminology used at the time; group records and
compare their chronology with actual Git history.

**With AI:** Ask: "What alternatives were discussed, what reasons were
recorded, and which decisions remain unresolved in these sources?" Verify
against the repository and current code.

**Outcome and lesson:** A human-reviewed design-history note. Historical intent
and current code are distinct sources of truth. Continuum does not automatically
ingest GitHub or maintain repositories.

## 7. Leaving a mail provider

**Situation:** Someone retires an old domain or switches providers.

**Without AI:** Independently back up mail first. Retrieve authorized folders
with read-only IMAP or import original EML. Preview and import a supported
MBOX export if necessary; reconstructed messages carry reconstruction
provenance. Check Inbox, Sent and other folders separately. Verify hashes,
compare coverage with the provider and test restoration on independent storage.

**With AI:** No AI is needed for migration. Later, optionally ask focused
questions about old contracts or renewal notices.

**Outcome and lesson:** A searchable local copy and independently tested backup.
Do not delete server copies merely because verification passed. Preservation
and recovery are deterministic tasks, not AI tasks.

## 8. Offline or resource-constrained research

**Situation:** A researcher travels without network access or wants to
reserve CPU and memory for other applications.

**Without AI:** Sync or import before departure. Open the archive locally,
search, inspect records and maintain notes. Existing indexed records remain
useful offline.

**With AI:** If an installed local model is available and resources permit,
enable Ollama for a focused session. Disable it afterward if unneeded.
Continuum does not download models.

**Outcome and lesson:** The same archive remains useful in either mode.
Fresh IMAP retrieval requires connectivity; local searching does not.
AI availability should not determine access to one's history.

## A reproducible object lesson

From the checkout, run the included synthetic multi-year demonstration:

```sh
python3 scripts/intelligence_demo.py
```

It uses a temporary fictional archive, not your personal records, and
demonstrates retrieval-only questions and a missing-topic question. Inspect
the evidence and formulate your own answer first.

If Ollama is running locally and the model is already installed:

```sh
python3 scripts/intelligence_demo.py --ask --model llama3.2
```

Substitute an installed model if necessary. Compare its claims with the
retrieval-only evidence. Record unsupported claims, missed records or
misleading inferences. Success means understanding what the evidence
supports, not merely receiving fluent prose.

## A repeatable research method

1. Define a narrow question and identify expected records.
2. Search identifiers, names, exact phrases and alternate vocabulary.
3. Check coverage, inspect originals and record gaps.
4. Organize records in collections and maintain a dated journal.
5. Optionally ask local AI a bounded question; inspect evidence, omissions,
   excerpts and citations.
6. Verify conclusions yourself; export only necessary records.
7. Back up and test restoration independently.

AI is disabled by default. Enable it deliberately in Preferences using a
loopback Ollama endpoint and an already installed model. See
[Intelligence](INTELLIGENCE.md). Retrieval works without Ollama.
Keyword retrieval may miss paraphrases. Citation identities are validated,
not the truth of claims. Model answers are derived research data.

## Privacy and practical boundaries

Use only records you may retain. Separate archives by person or trust boundary;
a unified archive is not access-controlled per mailbox. Optional SQLCipher
protects SQLite databases, not raw EML, external documents, configuration,
logs or exports. Use encrypted storage and independent encrypted backups;
see [Security model](SECURITY_MODEL.md). Never commit private archives to
GitHub. Review attachments and redact derivative copies, not originals.

PDF and DOCX extraction requires optional packages; image-only scans are
not OCR-indexed. Search limits, timeline caps and model context limits mean
results are not necessarily exhaustive. Keep original external documents
independently. Hashes detect changes relative to stored hashes, not
authenticity, completeness or certified chain of custody.

Continuum is a hobby project. Use the features that fit your needs; there
is no requirement to adopt every workflow or install AI.

*The intelligence is replaceable. The memories are yours.*
