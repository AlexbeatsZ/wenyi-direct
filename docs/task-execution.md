# Task execution modes

## Granular state machine

The normal `translate` command still advances a selected chapter through every enabled
quality gate. The `stage` command group exposes the same persisted work as six explicit
tasks:

| Command | Required broad phase | Result |
|---|---|---|
| `stage translate` | `translate` | saves the direct snapshot and stops at the next enabled audit |
| `stage factual-audit` | `factual_audit` | saves audit batches, validated terminology revisions, and repair regions; does not repair |
| `stage factual-repair` | `factual_audit` with `audit_complete=true` | repairs factual regions, validates fidelity, saves the factual snapshot |
| `stage chinese-audit` | `chinese_audit` | runs Chinese-only reading plus source-aware finding validation; does not repair |
| `stage chinese-repair` | `chinese_audit` with `audit_complete=true` | repairs validated language regions and performs the configured bounded recheck |
| `stage promote` | `promote` | performs final hard-term checks and atomically writes Formal text |

The broad `phase` remains compatible with the original pipeline. Granular readiness is
stored inside `factual_state.audit_complete` or `chinese_state.audit_complete`, and the
manifest's `task` field exposes the next command to operators and the monitor.

Audit tasks are idempotent. Completed windows and validations are reused. Repair tasks
are strict: they do not silently run a missing audit and they refuse a chapter in the
wrong phase.

## One-chapter stagger

`pipeline fast` implements two logical lanes:

```text
upstream lane:   translate -> factual-audit -> factual-repair
 downstream lane:             chinese-audit -> chinese-repair -> promote
```

The scheduler first completes upstream work for the first selected chapter. It then
runs downstream work for chapter N concurrently with upstream work for chapter N+1.
After the last upstream chapter completes, the scheduler drains its downstream work.

This is a true overlap of model calls, not merely interleaved logging. Tests use thread
barriers to require chapter 0 Chinese Reader execution and chapter 1 factual audit to
be active at the same time.

## Context safety

A next chapter must not inherit an uncorrected direct draft. During the overlap window,
its prompt may receive only the previous chapter's completed `stage_snapshots.factual`
text as provisional past context. The payload labels that material with
`provisional=true` and `factual_target`. Once the previous chapter reaches Formal, the
normal `formal_target` tail is used.

The same `past_context_chars` budget applies to provisional context. Future chapters
are never read.

## Mutation safety

The fast command holds the existing per-book process lock for the entire schedule, so
another CLI process cannot mutate the same run directory. Inside the process, a shared
re-entrant lock serializes manifest, event, artifact-input, audit, translation-artifact,
and usage writes. Shadow and Formal chapter files remain independent by chapter.

A lane failure propagates immediately. Already completed windows and regions remain
persisted, and the next invocation resumes from those checkpoints.
