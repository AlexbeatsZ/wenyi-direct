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
- Detailed scheduling and concurrency rules: `docs/design/stage-execution.md`.

## Active Work

- Granular stages and true two-thread chapter staggering are implemented.
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
- The formal chapter is replaced atomically only after structural checks and
  source-fidelity validation of every changed repair region. Keep proposals in
  shadow state and append every stage to `artifacts/`.
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

- Do not duplicate stage orchestration in a subclass or one CLI function per stage;
  the previous branch accumulated repeated validation calls and divergent behavior.
- In staggered execution, defer chapter N+1 terminology activation until chapter N
  finishes its downstream lane, or future knowledge can leak into N validation.
- Do not reintroduce a provider-wide Agy process lock: per-call temporary cwd plus
  `--new-project` is the isolation boundary, and a global lock makes two-lane mode
  silently sequential when all roles share one provider.
