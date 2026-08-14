# Wenyi Direct agent contract

## Goal

Maintain a chapter-first literary translator whose major stages can run independently
or through one resumable two-lane scheduler without weakening information boundaries
or atomic Formal promotion.

## Current State

- The canonical implementation is `DirectPipeline`; sequential, granular, and
  parallel orchestration share its stage dispatcher.
- CLI granular execution is `stage <name>`; end-to-end two-lane execution is
  `translate --parallel`.
- Existing Formal text can be re-audited without retranslation through resumable
  `review --parallel`; completed review generations require `--force` to reopen.
- Long-running CLI commands show overall and per-lane progress, and stream parsed
  audit, repair proposal, and validation results as one-line JSON records.
- Promotion resumes across the final Shadow/manifest commit window by reconciling a
  matching done Shadow with Formal; inconsistent targets remain blocked.
- Agy/Codex CLI calls retry recognized transient failures and use the configured
  fallback only after retries are exhausted; repair proposals resume directly at
  fidelity validation instead of repeating a completed repair call.
- Sol may veto a Gemini finding on the first repair call. After bounded validation
  disagreement, one persisted Sol final arbitration accepts a fidelity-safe result
  or skips the region while retaining the pre-repair target.
- Repair and arbitration prompts require every writable stable ID exactly once with
  a non-empty target; the stricter contract migrates old pending Shadows in place.
- Detailed scheduling and concurrency rules: `docs/design/stage-execution.md`.
- Reusable routes and per-route models live separately in one user-level catalog;
  project YAML is sparse and `use ROLE ROUTE MODEL` or one-run overrides select both
  aliases. Detailed rules: `docs/design/model-configuration.md`.

## Active Work

- Granular stages and true two-thread chapter staggering are implemented.
- Shared progress events and live audit JSON are implemented for sequential,
  two-lane, Formal-review, and independent-stage execution.
- The live Satisfaction review is 90 done / 2 pending. Chapter 71 is paused in
  Chinese audit and chapter 72 in factual audit; both Formal chapters are untouched.
- The current Gemini-compatible API does not support concurrent requests. During a
  parallel pair, chapter 72 factual batch 0 was persisted, but the same chapter 72
  response reached chapter 71's Chinese audit and failed its stable-ID boundary.
  Resume must use explicit CLI `--sequential`; `review` defaults to parallel.
- The live Satisfaction review keeps only book-specific settings in
  `C:/Users/Meta/Project/Workspaces/Satisfaction/config.wenyi-direct.yaml` and
  inherits `%APPDATA%/wenyi-direct/models.yaml`: `translate`/`factual_audit`/
  `chinese_audit`/`validation` select `deepseek-api/deepseek-v4-pro-max`;
  `repair` selects `codex/gpt-5.6-sol-high`. The route/model schema migration keeps
  the materialized runtime configuration identical after canonical JSON
  serialization, so existing review policy fingerprints remain valid.
- Codex's exact `You've hit your usage limit` response intentionally interrupts
  immediately without retry or fallback.
- The abandoned `agent/stage-commands-and-staggered-pipeline` branch is not the
  canonical implementation and must not be merged wholesale.

## Product identity

Wenyi Direct is a command-line long-form literary translator. It is intentionally
chapter-first: a strong model performs the first formal translation from source
text, then source-aware factual review and source-free Chinese reading review gate
promotion to the final text.

## Invariants

- Never require a whole-book prescan or synopsis before translation. Only already
  translated chapters may contribute long-range context; future chapters are not
  a prerequisite.
- Keep read scope and write scope separate. The model may see a full chapter or a
  source halo, but it may only return the explicitly requested stable segment IDs.
  Repair context expands read scope only; writable IDs require an audited or
  source-validated causal range.
- The Chinese-reader audit receives Chinese reader-visible text only. Source text,
  glossaries, model analysis, and translation instructions must not enter that call.
- A Chinese-reader finding is only repairable after a separate source-aware
  validation call. Repairs may span neighboring segments; never assume the symptom
  segment is the complete causal scope.
- The formal chapter is replaced atomically only after structural checks and a
  source-aware semantic gate for every changed repair region: ordinary Gemini
  fidelity validation or a bounded Sol final arbitration. Sol may instead skip a
  disputed region, which retains its pre-repair target. Keep proposals and rulings
  in Shadow state and append every stage to `artifacts/`.
- Only `active` hard terminology is mandatory; `active` preferred rules are advisory,
  while candidate/rejected rules never enter model prompts. Translation groups mean
  shared target fragments, never entity identity. Hard rules must pass deterministic
  checks after repair and before Formal promotion.
- Codex CLI business calls must remain ephemeral, read-only, and isolated from user
  rules. API keys belong in environment variables, never YAML or Git.
- Agy CLI business prompts use an isolated per-call UTF-8 temporary TXT file and a
  short sandboxed argv instruction; never put full chapter prompts on Windows argv.
- Model-discovered terminology is book-local state. Shared terminology configuration
  seeds new runs but is not mutated by translation.

## Build / Run / Test

- Use `uv` for Python commands.
- Add behavioral tests for information boundaries, stable IDs, repair expansion,
  resumability, provider argv/wire format, and final atomic promotion.
- Run `uv run ruff check .` and `uv run pytest` before committing.
- This repository reuses document/provider code from the MIT-licensed Wenyi project;
  keep `LICENSE` and provenance in the README.

## Durable Lessons

- Progress callbacks in two-lane mode must serialize only display delivery. Reusing
  the stage dispatcher keeps sequential and parallel output consistent, while a
  provider-wide lock would destroy actual overlap.

- `[active | Formal review migration | verified 2026-08-09]` A completed Formal chapter can coexist with a schema-1 translation Shadow left by an earlier run. Formal review must archive such a Shadow under `artifacts/superseded_shadows/` and seed its new review Shadow from Formal; treating the legacy candidate as resumable review state blocks valid re-audits, while silently overwriting it loses forensic state.

- Do not duplicate stage orchestration in a subclass or one CLI function per stage;
  the previous branch accumulated repeated validation calls and divergent behavior.
- In staggered execution, defer chapter N+1 terminology activation until chapter N
  finishes its downstream lane, or future knowledge can leak into N validation.
- Do not reintroduce a provider-wide Agy process lock: per-call temporary cwd plus
  `--new-project` is the isolation boundary, and a global lock makes two-lane mode
  silently sequential when all roles share one provider.
- Agy 1.1 on Windows may briefly retain its per-call cwd after returning success.
  Blank `request.txt` before cleanup and ignore only the residual directory-lock
  error; never convert a valid paid response into a pipeline failure.
- CLI `max_retries` is ineffective unless each CLI adapter consumes it. Classify
  only recognizable transient transport/runtime failures for retry/fallback;
  repeated malformed JSON may use the configured fallback. Codex's explicit current
  usage-limit response must interrupt immediately. Every explicit timeout, including
  a headless authentication timeout, must receive bounded retries; authentication
  failures without a timeout plus configuration, alignment, and quality failures
  must remain visible.
- Persist a repair proposal in Shadow before fidelity validation and mark an
  accepted proposal before returning it to the caller. Otherwise a validator or
  process interruption repeats an already paid repair call on resume.
- Cross-segment rewriting must not express a merge by returning an empty target:
  every writable stable ID remains a non-empty output slot. Tightening that model
  contract changes the policy fingerprint, so recognize the exact previous prompt
  fingerprint as an additive migration or an in-progress review will discard paid
  audit and accepted-region checkpoints.
- Formal is saved before the manifest is marked done, so a process exit may leave a
  done Shadow beside a pending manifest. Treat the done Shadow as the commit marker
  only after its complete stable-ID set and every translated target exactly match
  Formal; then reconcile the manifest without repeating paid stages.
- Legacy Formal states may lack `source_sha256`. Backfill it only after reparsing the
  current source and matching every chapter/segment index, source, kind, and anchor;
  never bypass the source-change guard with a blind manifest edit.
- A repair audit and its fidelity validator can disagree about source meaning. Sol
  therefore needs two explicit powers: veto an unfounded finding before validation,
  and make one final source-aware accept/skip ruling after bounded disagreement.
  Persist both the arbitration boundary and result so resume never restarts the
  paid conflict loop; the latest validator feedback supersedes contradictory old
  audit requirements in any intermediate repair prompt.
- Provider failure tests must use exact current CLI wording captured from live runs.
  Generic markers such as `quota exceeded` do not cover Codex's current
  `You've hit your usage limit` response; this exact response is deliberately tested
  as an immediate interruption without retry or fallback.
- Model connections must not be copied into each book config or conflated with model
  names. Keep user-named routes and their user-named models as separate levels in
  `models.yaml`; select both with `use ROLE ROUTE MODEL`. Use project `roles` only
  for a deliberate exception and one-run `--model ROLE=ROUTE/MODEL` for experiments.
  When migrating active resumable work, prove the fully resolved
  `Config.model_dump()` is unchanged so paid checkpoints are not invalidated.
