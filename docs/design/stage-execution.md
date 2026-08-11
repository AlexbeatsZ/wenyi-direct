# Stage execution and two-lane scheduling

## One state machine, two orchestration modes

The persisted chapter state has six executable stages:

| Stage | Requires | Stops after |
|---|---|---|
| `translate` | a pending chapter | complete direct Shadow text |
| `factual-audit` | translated Shadow | checkpointed findings and term candidates |
| `factual-repair` | complete factual audit | source-adjudicated factual snapshot |
| `chinese-audit` | factual snapshot | Chinese-only findings plus source validation |
| `chinese-repair` | complete Chinese audit | source-validated language repairs |
| `promote` | reviewed Shadow | atomic Formal replacement |

`DirectPipeline.run_stage()` executes exactly one ready stage and refuses missing
prerequisites. `DirectPipeline.run()` advances by repeatedly calling that same stage
dispatcher. The CLI therefore needs only `stage <name>` for granular work and
`translate` for composition; stage logic is not copied into command handlers.

## Re-auditing existing Formal text

`review` copies the current Formal targets into a new Shadow and starts at
`factual-audit`; it never calls the direct-translation role. Formal remains the
rollback baseline and changes only at the ordinary atomic `promote` gate.

```powershell
uv run wenyi-direct review book.epub --config config.yaml --parallel
```

Audit windows, source validations, repair regions, and the Formal baseline are
persisted, so repeating the same command resumes an interruption. Completed review
generations are skipped unless `--force` opens another generation. Chapters reopen
just before execution rather than all at once, preserving prior Formal context and
keeping later chapters formally complete until their turn.

Legacy states created before `source_sha256` was persisted may enter review only
after Wenyi Direct reparses the source and matches every chapter and segment's index,
source text, kind, and anchor. A complete match backfills the digest once; any
structural difference remains blocked as an explicit migration instead of silently
attaching Formal text to a different source.

When Formal text already exists, a pre-review Shadow from an older translation run
is not a valid review checkpoint. Starting Formal review archives that Shadow
verbatim under `artifacts/superseded_shadows/` before replacing it with a review
Shadow based on the current Formal text. This preserves unfinished legacy work
without allowing it to block or contaminate the review baseline.

Audit and repair are separate checkpoints. Re-running an unfinished audit reuses
completed windows and validations. Repair regions have stable persisted IDs, so a
crash resumes after accepted regions instead of repeating them.

Each repair proposal is also checkpointed before source-fidelity validation. If the
validator or process fails after the repair model has returned, resume validates the
same persisted proposal instead of paying for and potentially changing the repair a
second time. An accepted proposal is checkpointed before its region is committed, so
the final small commit window is resumable without another model call as well.

The Sol repair role is not subordinate to the Gemini finding. On its first call it
may return `reject_finding` with a source-based reason; the region then keeps its
pre-repair target and continues without a Gemini fidelity call. If Sol proposes a
repair and bounded Gemini validation still disagrees, Wenyi Direct persists an
`arbitration_required` checkpoint and gives Sol one final source-aware arbitration
call containing the original finding, rejected candidate, and latest validation
feedback. Sol may `accept` a final translation or `skip` the region. Its accepted
semantic ruling is not sent back into the same disagreement loop; deterministic
active-hard terminology checks still apply. A skip is an explicit completed result,
not a stage failure, and is recorded in the Shadow and audit artifacts.

Latest validation feedback replaces conflicting old audit wording in subsequent
repair prompts. Legacy pending proposals are preserved: the additive arbitration
policy fingerprint migrates in place, then the stored proposal is validated once
before any arbitration. Provider/runtime failures during arbitration remain ordinary
resumable interruptions and do not silently become content skips.

## Two-lane parallel mode

`translate --parallel` pipelines adjacent chapters with a one-chapter offset:

```text
warm up chapter N:  translate -> factual audit -> factual repair

in parallel:
  downstream N:    Chinese audit -> Chinese repair -> promote
  upstream N+1:    translate -> factual audit

join:               activate N+1 deferred term discoveries -> factual repair N+1
```

The overlap uses two worker threads and is tested with synchronization barriers; it
is not merely alternating log messages. Discontiguous chapter selections form
separate runs, so provisional context never jumps across an unselected gap.

## Information boundaries during overlap

Chapter N+1 may use only chapter N's completed factual snapshot as provisional past
context. The payload marks this material with `provisional=true` and
`factual_target`; an unreviewed direct draft is never exposed as past knowledge.
Once N reaches Formal, ordinary `formal_target` context is used.

Term candidates found in N+1 are checkpointed but not activated while N's downstream
lane is running. They are activated only after both lanes join, before N+1 factual
repair. This prevents a future chapter's discovery from entering N source validation
or changing N's policy fingerprint.

The Chinese Reader call itself remains unchanged: it receives reader-visible Chinese
only, without source, terminology, analysis, or translation instructions.

## Mutation and failure safety

The existing per-book process lock covers the complete sequential or parallel run.
Within a parallel process, manifest read-modify-write operations and shared JSONL or
input-artifact writes use a re-entrant thread lock. Shadow and Formal files are still
owned by one chapter lane at a time.

A worker failure is propagated at the join. Completed windows, audit results, repair
regions, and Shadow targets are already persisted, so the next invocation resumes
through the same stage dispatcher. Formal text changes only in `promote`.

CLI providers retry only recognized transient transport/runtime failures according
to `max_retries`. If those retries are exhausted and
`roles.content_policy_fallback` is configured, the same request is sent to that
fallback. Repeated malformed JSON also routes to the fallback only after primary
retries are exhausted. Codex's explicit `You've hit your usage limit` response is a
permanent runtime interruption: it is neither retried nor sent to the fallback.
Permanent authentication, configuration, alignment, and quality-gate failures remain
explicit errors. An explicit timeout is always transient, including authentication
flows that time out; an authentication failure without a timeout remains permanent.

Promotion uses the persisted `done` Shadow as its commit marker. If the process exits
after the Formal chapter and done Shadow are saved but before the manifest status is
updated, resume compares every translated stable ID and Formal target with that Shadow
before completing the manifest update without another model call. A mismatch remains
an explicit state error rather than being silently accepted.

## CLI progress and live audit records

`DirectPipeline` emits transport-neutral progress events from the shared stage
dispatcher and from its window/repair loops. The CLI renders one book-level chapter
bar plus one active row per chapter, so dual-lane execution exposes both workers
instead of looking sequential. Callback delivery is serialized because two worker
threads may finish model calls simultaneously; this serialization covers display
only and does not lock provider calls.

Audits and repair validation additionally emit one-line JSON records as soon as a
result has been parsed and accepted into pipeline state. Records include chapter,
stage, event type, stable IDs and issue details. Repair records include the original
issues, proposed before/after text, and the fidelity verdict. These are projections
of already persisted results, not extra model calls or prompt contents. In particular,
the Chinese Reader still receives Chinese reader-visible text only; its later
source-aware validation is reported as a separate event.
