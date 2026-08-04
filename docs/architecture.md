# Architecture

## Why the pipeline starts with the strongest translation call

The first formal translation establishes subjects, reference, dialogue ownership,
word sense, and Chinese sentence structure. A cheap draft can anchor later models to
a fluent but incorrect frame, so Wenyi Direct does not create one. The first model
sees unpolluted source context and produces the candidate directly.

For Japanese source, the translation prompt states general structural risks rather
than book-specific answers: omitted subjects, nominal fragments, postposed material,
relative-clause order, discourse function, counters, and the temptation to expand a
physical meaning for fluency. Literary fragments may keep their rhythm, but they may
not become invalid Chinese collocations or lose question, command, or quotation
function.

The project also does not require reading the whole book first. Human translators
work on serial fiction and newly released chapters all the time. A current chapter,
nearby source, and a bounded raw tail from completed chapters are enough to translate
without treating future revelations as a prerequisite.

## Data and control flow

```text
document parser
  -> stable chapter/segment IDs
  -> strong direct translation into Shadow
  -> factual audit (source + Shadow)
  -> causal-region repair + source validation
  -> Chinese Reader Audit (Chinese only)
  -> finding validation (neighboring source + Chinese)
  -> causal-region repair + source validation
  -> atomic Shadow-to-Formal promotion
  -> format assembly and container validation
```

Fresh calls provide task and information separation. They are not described as
independent reviewers when the configured roles use the same model.

## Read scope versus write scope

A model call receives two explicit scopes:

- `READ_ONLY`: surrounding source needed for subjects, speakers, postposed
  explanations, and word sense.
- `WRITE`: the only stable IDs the response may contain.

If output is too large or structurally invalid, only `WRITE` is bisected. The broad
source evidence stays intact. A short chapter is read as a whole; a long chapter
uses source on both sides of the writable range. Previous Chinese is never used as
a substitute for nearby source evidence.

## Chinese Reader Audit boundary

`chinese_reader_messages()` constructs its payload only from final Chinese target
strings and stable IDs. It has no parameter for source text, glossary, analysis, or
source metadata. Its system prompt explicitly frames the call as pre-release quality
acceptance of a machine-translated manuscript, so the reviewer actively looks for
literal but invalid Chinese instead of reading as an ordinary literary critic. This
makes the requested reading order mechanical:

1. detect actual Chinese reading problems;
2. for each finding, open a separate source-aware validation request;
3. repair only findings that can be fixed without changing meaning.

The reported segment is a symptom, not an assumed repair boundary. `RepairPlanner`
combines the symptom, any model-approved causal range, and overlapping issues into the
write scope. Neighboring segments are added only to the read scope; context is never
silently converted into permission to rewrite more text.

## State and recovery

`state/<book>/chapters/` contains Formal chapters. `shadows/` contains the resumable
candidate and phase. Assembly reads only Formal chapters. Model inputs are content-
addressed under `artifacts/inputs/`; translation proposals and accepted stages are
append-only JSONL, as are audits and events. A crash can repeat an audit but cannot
expose a half-reviewed chapter as final output.

Each shadow records a fingerprint of model routing, pipeline configuration, prompt
contracts, and the current terminology snapshot. Resuming after one of those changes
invalidates downstream review checkpoints while preserving already completed direct
translation. Low-level JSON and document exporters independently reject incomplete
Formal state; this is not only a CLI check.

## Terminology

The configured `terminology.yaml` is the seed for a new book. Each run copies that
seed to `state/<book>/terminology.yaml`; model discoveries are written only to this
book-local snapshot and cannot contaminate another book. A terminology document
contains only `groups` and `terms`. A group stores the source and
target fragments shared by explicitly linked expressions; it is not a person, place,
event, or entity record. Terms are retrieved only when active, within their optional
chapter range, and actually present in the current source scope. Longer matches take
priority over nested shorter matches.

`mode` and `status` are independent: hard rules are mechanically enforced while
preferred rules are suggestions; active rules participate while candidate/rejected
rules never enter prompts. The factual audit may discover stable names, setting
expressions, and ordinary noun phrases such as a repeatedly referenced shop when they
serve a stable translation function, at no extra model-call cost. Frequency alone is
not enough. A non-conflicting discovery becomes `active + preferred`; a conflict
becomes `candidate`, and an exact rejected mapping is not proposed again.

Pronoun guidance is deliberately term-local. It enters knowledge when that term's
source expression is present in the current read scope and does not create an active
character, speaker, or cross-window coreference tracker.

Hard terminology is deterministically checked after repair and again before Formal
promotion. A violation found at promotion enters the same repair and fidelity gate,
even when optional audits are disabled; resuming cannot loop on an unchanged
promotion error. Repair and fidelity-validation prompts receive the same current
terminology snapshot as direct translation and factual audit. The Chinese Reader
Audit remains Chinese-only and therefore receives none of it.

There is no model-generated whole-book bible in the default path.

The prompt may also receive a small raw tail from already completed chapters:
original segments first, their accepted Chinese second. This preserves the
translator's ordinary memory of prior events without inventing an authoritative
synopsis or requiring unreleased chapters.
