# Wenyi Direct

Wenyi Direct is a new, simpler literary-translation pipeline derived from the
engineering shell of [Wenyi](https://github.com/BigDawnGhost/wenyi). It deliberately
does not use cheap draft translation, mandatory whole-book analysis, default
polishing, back-translation, or blind re-review.

Its quality path is:

1. A strong model directly translates a full chapter whenever the configured
   context/output budget permits. Long chapters use a bidirectional source halo.
2. A source-aware factual audit reports concrete meaning problems; related findings
   are repaired over their explicit causal region and verified against the source.
   Neighboring context remains read-only.
3. A Chinese Reader Audit sees only reader-visible Chinese. Its findings are then
   checked against neighboring source before any repair, followed by source-fidelity
   validation of changed regions.
4. The reviewed shadow chapter atomically becomes formal text. Every proposal and
   input snapshot remains append-only under the run's `artifacts/` directory.

No future chapter is required. A bounded raw tail from completed chapters supplies
past-only context as a serial work progresses.

Terminology supports translation-sharing groups plus per-term `mode`, `status`,
chapter range, and optional pronoun guidance. Factual audit can discover stable names,
setting expressions, and ordinary noun phrases that repeatedly need one stable
translation without another model call. Non-conflicting discoveries become soft
active preferences, conflicts remain inactive candidates, and rejected mappings stay
out. Pronoun guidance remains local to a retrieved term; the pipeline does not infer
or persist an active speaker/referent across later windows. Hard rules are checked
mechanically during repair and before final promotion. Model discoveries live in the
current book's state rather than the shared terminology seed file.

## Supported model transports

- `codex-cli`: isolated `codex exec --ephemeral --ignore-user-config --ignore-rules`
- `agy`: sandboxed temporary-TXT requests with a short `agy --print` instruction
- `openai-compatible`: `/chat/completions` services
- `anthropic-compatible`: Anthropic Messages-compatible `/v1/messages` services

Each stage may use a different transport, or all stages may share one model. API
keys are named by `api_key_env` in YAML and supplied through environment variables.

## Independent stages and parallel execution

Every major stage can be executed independently. A stage stops at its declared
boundary and refuses to invent missing prerequisites:

```powershell
uv run wenyi-direct stage translate path\to\book.epub --chapters 0-3 --config config.yaml
uv run wenyi-direct stage factual-audit path\to\book.epub --chapters 0-3 --config config.yaml
uv run wenyi-direct stage factual-repair path\to\book.epub --chapters 0-3 --config config.yaml
uv run wenyi-direct stage chinese-audit path\to\book.epub --chapters 0-3 --config config.yaml
uv run wenyi-direct stage chinese-repair path\to\book.epub --chapters 0-3 --config config.yaml
uv run wenyi-direct stage promote path\to\book.epub --chapters 0-3 --config config.yaml
```

For normal end-to-end work, `translate --parallel` uses two real model lanes: chapter
N completes Chinese review and promotion while chapter N+1 translates and performs
factual audit. The next chapter sees only N's completed factual snapshot as
provisional past context. Future-chapter terminology discoveries remain deferred
until N is Formal.

The single `stage <name>` command replaces the earlier standalone factual-only
`audit` command and avoids six duplicated CLI handlers. See
[docs/design/stage-execution.md](docs/design/stage-execution.md) for checkpoint,
locking, and information-boundary details.

## Quick start

```powershell
uv sync
uv run wenyi-direct init-config config.yaml
# edit config.yaml and set the referenced API-key environment variable if needed
uv run wenyi-direct translate path\to\book.epub --config config.yaml
uv run wenyi-direct translate path\to\book.epub --parallel --config config.yaml
uv run wenyi-direct status path\to\book.epub --config config.yaml
uv run wenyi-direct monitor path\to\book.epub --config config.yaml
uv run wenyi-direct assemble path\to\book.epub --config config.yaml --format epub

# terminology lifecycle
uv run wenyi-direct terms group-add flame 炎 火焰 --config config.yaml
uv run wenyi-direct terms add 炎魔法 火焰魔法 --group flame --mode hard --config config.yaml
uv run wenyi-direct terms set-status 炎魔法 active --config config.yaml
```

Inputs: EPUB, FB2, TXT, Markdown, HTML, PDF, and the documented JSON interchange
format. Outputs: EPUB, TXT, Markdown, HTML, or JSON.

See [docs/architecture.md](docs/architecture.md) and
[docs/providers.md](docs/providers.md) for the precise boundaries and configuration.
The [format reference](docs/formats.md) documents JSON/game-script interchange.
Sanitized methods, compact results, and selected excerpts from model comparisons are
published in [experiments/](experiments/README.md).

## Provenance

Document ingestion/assembly, resumable storage, and the Codex/Agy/OpenAI-compatible
adapter foundations are reused from Wenyi under the included MIT license. The
translation policy, prompts, repair planning, Chinese-only audit boundary,
Anthropic-compatible adapter, and command-line orchestration are new here.
