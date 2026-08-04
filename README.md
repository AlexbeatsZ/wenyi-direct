# Wenyi Direct

Wenyi Direct is a focused literary-translation pipeline derived from the engineering
shell of [Wenyi](https://github.com/BigDawnGhost/wenyi). It deliberately avoids a
cheap draft, mandatory whole-book analysis, default polishing, back-translation, and
blind final re-review.

Its quality path is:

1. A strong model directly translates a full chapter whenever the configured
   context/output budget permits. Long chapters retain source on both sides of the
   writable range.
2. A checkpointed source-aware factual audit reports concrete meaning problems and
   may challenge existing terminology. Approved whole-rule terminology corrections
   are migrated immediately across existing Formal and Shadow text.
3. Remaining factual findings are merged into causal repair ranges and verified
   against the source.
4. A Chinese Reader Audit sees only reader-visible Chinese. Its findings are checked
   against nearby source, globally merged into non-overlapping repair ranges, repaired,
   fidelity-validated, and optionally rechecked once in Chinese.
5. The reviewed Shadow chapter atomically becomes Formal text. Every proposal, input
   snapshot, audit, migration, and accepted stage remains traceable under the run's
   state directory.

No future chapter is required. A strictly bounded raw tail from completed chapters
supplies past-only context as a serial work progresses.

## Terminology

Terminology supports translation-sharing groups plus per-term `mode`, `status`,
chapter range, and optional pronoun guidance.

- Translation and repair calls treat active `hard` rules as constraints and active
  `preferred` rules as suggestions.
- Reviewers receive those mappings as challengeable current conventions. An incorrect
  hard rule is therefore not immune to factual review.
- Model discoveries begin as inactive candidates. They become active preferences only
  after their supporting chapter reaches Formal and still contains the same mapping.
- A separately validated whole-rule correction is migrated immediately by
  source-anchored search. Deterministic one-to-one occurrences are changed directly;
  ambiguous live occurrences are repaired by the model and fidelity-validated in the
  same migration. Historical snapshots that cannot be migrated safely are invalidated.

All discoveries and revisions live in `state/<book>/terminology.yaml`; the shared seed
file is never mutated by translation.

## State and explicit restart

Normal translation resumes exactly from persisted phases, audit windows, validations,
and completed repair regions. Prompt, model, configuration, or terminology changes do
not automatically discard paid work.

Use an explicit restart when you want to rerun a stage:

```powershell
uv run wenyi-direct translate path\to\book.epub --config config.yaml \
  --restart-from translate
uv run wenyi-direct translate path\to\book.epub --config config.yaml \
  --restart-from factual-audit
uv run wenyi-direct translate path\to\book.epub --config config.yaml \
  --restart-from chinese-audit
```

Source-file and chapter digests remain only to prevent stale segmented state from
silently resuming against changed input.

## Granular tasks

Every major task can be run independently. Audit-only commands persist their findings
and stop before any repair model is called. Repair commands require the corresponding
audit checkpoint and refuse to guess or silently run a missing earlier task.

```powershell
uv run wenyi-direct stage translate path\to\book.epub --chapters 0-3 --config config.yaml
uv run wenyi-direct stage factual-audit path\to\book.epub --chapters 0-3 --config config.yaml
uv run wenyi-direct stage factual-repair path\to\book.epub --chapters 0-3 --config config.yaml
uv run wenyi-direct stage chinese-audit path\to\book.epub --chapters 0-3 --config config.yaml
uv run wenyi-direct stage chinese-repair path\to\book.epub --chapters 0-3 --config config.yaml
uv run wenyi-direct stage promote path\to\book.epub --chapters 0-3 --config config.yaml
```

The persisted `phase` remains the broad state-machine phase. The manifest's `task`
field shows the next granular command, such as `factual-repair` or `chinese-repair`.
Re-running a completed audit task is idempotent and reuses its stored batches.

## Staggered fast pipeline

```powershell
uv run wenyi-direct pipeline fast path\to\book.epub --chapters 0-20 --config config.yaml
```

This mode uses two model lanes with a one-chapter offset:

```text
chapter N:   Chinese audit -> Chinese repair -> promote
chapter N+1: translate -> factual audit -> factual repair
```

The next chapter may use the previous chapter's completed **factual snapshot** as
provisional past context while the previous chapter is undergoing Chinese-only review.
It never receives the previous chapter's uncorrected direct draft. Once the previous
chapter reaches Formal, normal Formal past context is used again.

The command holds the book's process lock for the entire run, so a second CLI process
cannot mutate the same book concurrently. Within that process, only independent
chapter work overlaps; shared manifest, event, artifact, and usage writes are
serialized. A failed lane stops the pair and leaves both chapters at their persisted
checkpoints for normal resume.

## Supported model transports

- `codex-cli`: isolated `codex exec --ephemeral --ignore-user-config --ignore-rules`
- `agy`: sandboxed temporary-TXT requests with a short `agy --print` instruction;
  only stdout is accepted as model output
- `openai-compatible`: `/chat/completions` services
- `anthropic-compatible`: Anthropic Messages-compatible `/v1/messages` services

Each stage may use a different transport, or all stages may share one model. API keys
are named by `api_key_env` in YAML and supplied through environment variables.

## Quick start

```powershell
uv sync
uv run wenyi-direct init-config config.yaml
# edit config.yaml and set the referenced API-key environment variable if needed
uv run wenyi-direct translate path\to\book.epub --config config.yaml
uv run wenyi-direct status path\to\book.epub --config config.yaml
uv run wenyi-direct monitor path\to\book.epub --config config.yaml
uv run wenyi-direct assemble path\to\book.epub --config config.yaml --format epub

# fastest two-lane chapter schedule
uv run wenyi-direct pipeline fast path\to\book.epub --config config.yaml

# terminology seed management
uv run wenyi-direct terms group-add flame 炎 火焰 --config config.yaml
uv run wenyi-direct terms add 炎魔法 火焰魔法 --group flame --mode hard --config config.yaml
uv run wenyi-direct terms set-status 炎魔法 active --config config.yaml

# immediately migrate a confirmed book-local whole-rule correction
uv run wenyi-direct terms revise path\to\book.epub \
  --source 黒炎 --old-target 黑色火焰 --new-target 黑炎 \
  --config config.yaml
```

An explicit terminology migration changes deterministic occurrences directly and, by
default, immediately sends ambiguous existing wording through repair plus fidelity
validation. Add `--no-model` to make ambiguity leave all text unchanged and only emit a
saved migration plan with exact affected locations.

Inputs: EPUB, FB2, TXT, Markdown, HTML, PDF, and the documented JSON interchange
format. Outputs: EPUB, TXT, Markdown, HTML, or JSON.

See [docs/architecture.md](docs/architecture.md) and
[docs/providers.md](docs/providers.md) for precise boundaries and configuration. The
[format reference](docs/formats.md) documents JSON/game-script interchange. Sanitized
methods, compact results, and selected excerpts from model comparisons are published
in [experiments/](experiments/README.md).

## Provenance

Document ingestion/assembly, resumable storage, and the Codex/Agy/OpenAI-compatible
adapter foundations are reused from Wenyi under the included MIT license. The direct
translation policy, scoped repair planning, Chinese-only audit boundary,
Anthropic-compatible adapter, terminology revision migration, and current command-line
orchestration are new here.
