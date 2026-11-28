# Intelligence workspace

HB Continuum can answer questions about your documented history using only
records in the local archive. Retrieval and evidence inspection always work;
a language model is optional and local.

**The model is replaceable. The memories are yours.**

For realistic examples of when to use AI and when ordinary search is sufficient, see the [practical field guide](USE_CASES.md). AI is never a prerequisite for accessing your archive.

## Workflow

1. You ask a natural-language question in the **Intelligence** workspace.
2. The engine turns it into bounded FTS5 retrieval queries and searches the
   selected sources, applying date, participant/actor and collection filters.
   Short questions first require all meaningful terms (for example
   `"silver" AND "invoice"`), which avoids returning every record that happens
   to mention only one word. A broader OR query is used only when the stricter
   query finds nothing, and the result is marked so partial evidence is not
   mistaken for a full term match.
3. Matches are deduplicated and ranked across queries. Each surviving record
   is loaded from the archive or catalog with its stable identifier, source
   location, timestamp and original body.
4. A bounded context is assembled. Records are numbered `E1`, `E2`, ...;
   excerpts are marked, and records omitted for budget reasons are counted.
   The final prompt is measured against `ai_max_input_chars`; context is
   reduced rather than silently sending an oversized request.
5. If local Ollama inference is enabled, the evidence JSON and your question
   are sent to the loopback endpoint with explicit instructions to treat
   evidence as untrusted data.
6. The model must return structured claims with evidence identifiers. The
   engine validates every identifier against the evidence actually supplied.
   Unknown identifiers are rejected and displayed; claims without accepted
   evidence are flagged.
7. You inspect the answer, claims, contradictions, missing information and
   the underlying evidence. A citation opens the original message or catalog
   record, highlights the evidence row and offers **Back to answer**.

No model is consulted when retrieval finds no evidence, and a model answer is
never written back into the archive. A missing-topic question such as "Was a
final refund recorded?" returns no evidence rather than generic records that
merely contain a common word.

## Practical follow-up questions

When a conversation is selected, its previous question and filters are
restored in the workspace. A short follow-up such as "What happened next?"
does not need to repeat the topic: if normal retrieval finds nothing, the
engine tries the previous user question and marks that fallback in the result.
The conversation transcript is also supplied to the model as continuity only,
not as evidence.

## What is stored where

- `archive.sqlite3` and `catalog.sqlite3` remain the preserved evidence.
- `research.sqlite3` schema 6 stores research conversations and turns:
  questions, derived answers, citations, model names and timestamps.
- Deleting a conversation deletes only derived conversation data. It never
  deletes or modifies a source record, attachment or original file.
- Conversation export writes a JSON document outside the archive.

## Evidence and citations

Every supplied record carries:

- an evidence identifier (`E1`, `E2`, ...),
- the source (`email`, `facebook` or `document`) and source type,
- the original record identifier (SHA-256 hash or catalog record id),
- title, participant/actor, declared date and source location,
- an excerpt and whether it is the complete stored body or a truncation,
- the retrieval query set that found it.

The model may only cite identifiers from that list. Invalid identifiers and
uncited claims are surfaced in the interface. A plain-text model reply with no
structured claims is shown as unstructured and is not presented as grounded.

Claim kinds are limited to `documented`, `multiple`, `inference`, `uncertain`
and `contradiction`. The model is instructed to present disagreement rather
than silently choosing a side, and to state when evidence is insufficient.
These labels describe the model's structured output; they are not independent
proof of factual correctness.

## Local Ollama settings

AI is disabled by default. Enable it in Preferences with:

- `ai_enabled`: true
- `ai_backend`: `ollama`
- `ai_endpoint`: an `http(s)` loopback address such as
  `http://127.0.0.1:11434`
- `ai_model`: an already-installed Ollama model name
- `ai_timeout`, `ai_max_input_chars`: request bounds. The research workspace
  shows the models reported by the local instance and allows a model name to
  be typed or selected; checking the model does not download anything.

The client refuses redirects and non-loopback endpoints, never pulls or
downloads a model, and streams responses with cancellation. It requests JSON
output from Ollama and still treats malformed or unstructured output as
ungrounded rather than inventing citations. Model discovery lists what the
local Ollama instance already has. The application remains a complete archival
and research workstation when Ollama is absent, disabled or unreachable;
retrieval still returns inspectable evidence.

## Prompt-injection boundary

Imported messages and records are untrusted data. Evidence is supplied as a
JSON array of records and the system instruction explicitly says never to obey
text inside it. Conversation history is labeled as untrusted continuity text,
not evidence or instructions. The research engine has no tool access, no shell,
no filesystem or archive mutation capability, and never turns model output into
an action.
Prompt injection can still influence what a model says; it cannot make this
engine modify evidence.

## Limits

- Retrieval is keyword/FTS5 based, not semantic or embedding-based. Paraphrases
  that share no terms may be missed.
- The archive cannot prove undocumented events, intentions or beliefs. A
  single historical message does not establish a permanent view.
- Contradiction and uncertainty labels are model-reported and validated only
  for citation identity, not for semantic truth.
- Context and per-record limits deliberately truncate long material. Omitted
  and excerpted evidence is reported, never silently hidden as complete.
  `ai_max_input_chars` is enforced for the prompt as well as the evidence
  budget.
- The answer arrives when the model stream finishes; per-token UI streaming is
  not implemented. Progress text, job cancellation and bounded retrieval are
  present.
- No interactive desktop acceptance run is claimed by the automated tests.
- The synthetic demo in `scripts/intelligence_demo.py` is a supported smoke
  path; it is not a substitute for reviewing your own archive.


## Synthetic multi-year demonstration

The repository includes a self-contained fictional archive for practical
research exercises:

```sh
python3 scripts/intelligence_demo.py
```

It creates a temporary archive with correspondence and documents dated
2019–2023, runs retrieval-only questions about a disputed invoice, a heritage
grant and a garden document, demonstrates a follow-up question and shows a
missing-topic question returning no evidence. It never opens a real archive.
Local inference is opt-in:

```sh
python3 scripts/intelligence_demo.py --ask --model llama3.2
```

The demo only calls the supplied loopback Ollama endpoint; it does not pull
models or contact a remote service.
