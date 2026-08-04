# Architecture

## Why the pipeline starts with the strongest translation call

The first formal translation establishes subjects, reference, dialogue ownership,
word sense, and Chinese sentence structure. A cheap draft can anchor later models to
a fluent but incorrect frame, so Wenyi Direct does not create one. The first model
sees unpolluted source context and produces the candidate directly.

For Japanese source, the translation prompt states a small set of general structural
risks rather than a long linguistic checklist or book-specific answers: omitted
subjects, postposed material, relative-clause order, discourse function, counters,
and the temptation to expand a physical meaning for fluency. Literary fragments may
keep their rhythm, but they may not become invalid Chinese collocations or lose
question, command, or quotation function.

The project does not require reading the whole book first. A current chapter, nearby
source, and a strictly bounded raw tail from completed chapters are sufficient for
serial fiction and unfinished works. Future revelations are never a prerequisite.

## Data and control flow

```text
document parser
  -> stable chapter/segment IDs
  -> strong direct translation into Shadow
  -> checkpointed factual audit (source + Shadow)
       -> validate any proposed whole-rule terminology revision
       -> immediately migrate approved revisions across existing Formal/Shadow text
       -> merge and repair remaining factual findings
  -> Chinese Reader Audit (Chinese only)
  -> source-aware validation of reader findings
  -> globally merge overlapping repair ranges
  -> repair + source-fidelity validation
  -> at most one Chinese-only recheck of changed regions
  -> atomic Shadow-to-Formal promotion
  -> confirm only discovered terminology that survived into Formal
  -> format assembly and container validation
```

Fresh calls provide task and information separation. They are not described as
independent reviewers when configured roles use the same model.

## Read scope versus write scope

A model call receives two explicit scopes:

- `READ_ONLY`: surrounding evidence needed for subjects, speakers, postposed
  explanations, word sense, and continuity.
- `WRITE`: the only stable IDs the response may change.

If a direct-translation response is structurally invalid, only `WRITE` is bisected.
The broad source evidence remains available. Every split records its reason, parent
range, child ranges, and depth, so one full-chapter call is distinguishable from a
multi-call fallback in experiments.

Repair context expands only the read scope. Writable IDs must come from an audited or
source-validated causal range. Overlapping Chinese-reader repair ranges are merged
globally before any language repair, so two batches cannot independently overwrite
the same segment using stale pre-repair findings.

## Chinese Reader Audit boundary

`chinese_reader_messages()` constructs its payload only from reader-visible Chinese
strings and stable IDs. It has no source, glossary, source-language, analysis, or
source-derived metadata parameter. The prompt frames the input as a machine-translated
manuscript so the reviewer actively looks for literal but invalid Chinese rather than
assuming every odd expression is authorial style.

The reading order is mechanical:

1. detect concrete Chinese reading problems;
2. validate each finding against nearby source in a separate call;
3. merge all accepted causal ranges;
4. repair each non-overlapping range and validate source fidelity;
5. optionally perform one Chinese-only recheck of changed regions.

`pipeline.max_language_rechecks` is restricted to `0` or `1`; there is no unbounded
self-editing loop.

## State and recovery

`state/<book>/chapters/` contains Formal chapters. `shadows/` contains the resumable
candidate, explicit phase, stage snapshots, window checkpoints, validated findings,
and completed repair-region IDs. Assembly reads only Formal chapters. Model inputs
are content-addressed under `artifacts/inputs/`; proposals, accepted stages, audits,
and events remain append-only.

Resume never infers user intent from prompt, model, configuration, or terminology
hashes. The default command continues exactly from persisted progress. Users
explicitly request discarded work when desired:

```powershell
wenyi-direct translate book.epub --restart-from translate
wenyi-direct translate book.epub --restart-from factual-audit
wenyi-direct translate book.epub --restart-from chinese-audit
```

Direct and factual stage snapshots make later-stage restarts deterministic. Restarting
from direct translation clears Shadow work but does not overwrite the existing Formal
chapter until the new candidate passes every gate. Legacy states without the required
snapshot must restart from `translate` rather than guessing a reconstruction.

Source-file SHA-256 and per-chapter source digests remain. They protect segmented state
from silently resuming against changed input; they are not policy or progress hashes.

Factual-audit windows and repair regions are checkpointed independently. Chinese
reader batches, source validations, globally merged repair regions, and the bounded
language recheck are also checkpointed. A crash may repeat only the unfinished unit,
not every earlier call in the chapter.

## Terminology roles and lifecycle

The configured `terminology.yaml` is a seed for a new book. Each run copies it to
`state/<book>/terminology.yaml`; all model discoveries and rule revisions are
book-local.

A terminology document contains translation-sharing groups and term rules. Groups
store source/target fragments shared by explicitly linked complete expressions; they
are not entity records. Terms participate only when active, inside their optional
chapter range, and actually present in current source. Longer source matches take
priority over nested shorter rules.

`mode` and `status` are independent:

- `hard` and `preferred` describe generation-stage strength;
- `active`, `candidate`, and `rejected` describe lifecycle state.

Translation and repair calls receive active hard/preferred constraints. Factual audit,
Chinese-finding validation, and source-fidelity validation receive the same mappings
as challengeable `current_terminology`. A hard label therefore cannot make an
incorrect historical decision immune to review. The Chinese Reader itself still sees
no terminology or source information.

Model-discovered mappings are always stored first as inactive `candidate + preferred`
rules with a supporting chapter/segment. After that chapter passes every quality gate,
the candidate becomes active only if the final Formal evidence still contains the
same source term and target form. If review changed the wording, the candidate remains
inactive. Manual candidates are never auto-promoted.

## Whole-rule terminology revision and migration

A factual auditor may propose `scope=entire_existing_rule` only when evidence indicates
that the complete current rule is wrong, rather than merely inapplicable in one
context. A separate validation call must approve the proposal before any state change.
If evidence suggests polysemy, range splitting, or a local exception, the proposal is
rejected as a whole-rule migration and remains an ordinary factual issue.

Approved revisions are migrated immediately; the system does not wait for later
chapters to rediscover each occurrence:

1. locate affected segments from source-side longest-match selection;
2. mechanically replace one unambiguous old target with the new target;
3. leave unrelated Chinese text untouched even when it contains the same words;
4. send ambiguous live occurrences to a focused repair call and source-fidelity gate;
5. update Formal, Shadow, and mechanically safe stage snapshots;
6. invalidate a historical snapshot when it cannot be migrated safely;
7. replace the terminology rule only after every text edit succeeds.

Migration plans and results are persisted under `term_migrations/`. Without a model
resolver, an ambiguous migration applies nothing and reports the exact chapter,
segment, storage layer, and occurrence counts. Users can also trigger a confirmed
whole-rule migration explicitly:

```powershell
wenyi-direct terms revise book.epub \
  --source 黒炎 --old-target 黑色火焰 --new-target 黑炎 \
  --config config.yaml
```

Historical append-only artifacts are not rewritten; a new `term_migration` stage
records before/after text instead.

## Hard terminology gates

Active hard terminology is checked mechanically after ordinary repair and before
Formal promotion. Repair calls receive enforceable terminology, while fidelity calls
may still report that an existing rule itself conflicts with source evidence. An
approved whole-rule revision is migrated before stale hard-rule checks are applied,
so the old rule cannot force the repaired text back to an acknowledged error.

There is no model-generated whole-book bible or active cross-window speaker tracker in
the default path. Pronoun guidance remains local to a retrieved source term.
