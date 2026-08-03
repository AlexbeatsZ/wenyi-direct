# Wenyi Direct agent contract

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
- The Chinese-reader audit receives Chinese reader-visible text only. Source text,
  glossaries, model analysis, and translation instructions must not enter that call.
- A Chinese-reader finding is only repairable after a separate source-aware
  validation call. Repairs may span neighboring segments; never assume the symptom
  segment is the complete causal scope.
- The formal chapter is replaced atomically only after structural checks and
  source-fidelity validation of every changed repair region. Keep proposals in
  shadow state and append every stage to `artifacts/`.
- Only human-confirmed hard terms are mandatory. Suggested terms and inferred facts
  are advisory and must retain evidence and visibility boundaries.
- Codex CLI business calls must remain ephemeral, read-only, and isolated from user
  rules. API keys belong in environment variables, never YAML or Git.

## Development and verification

- Use `uv` for Python commands.
- Add behavioral tests for information boundaries, stable IDs, repair expansion,
  resumability, provider argv/wire format, and final atomic promotion.
- Run `uv run ruff check .` and `uv run pytest` before committing.
- This repository reuses document/provider code from the MIT-licensed Wenyi project;
  keep `LICENSE` and provenance in the README.
