# Wenyi Direct

Wenyi Direct is a new, simpler literary-translation pipeline derived from the
engineering shell of [Wenyi](https://github.com/BigDawnGhost/wenyi). It deliberately
does not use cheap draft translation, mandatory whole-book analysis, default
polishing, back-translation, or blind re-review.

Its quality path is:

1. A strong model directly translates a full chapter whenever the configured
   context/output budget permits. Long chapters use a bidirectional source halo.
2. A source-aware factual audit reports concrete meaning problems; related findings
   are repaired together over an expanded region and verified against the source.
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
mechanically during repair and before final promotion.

## Supported model transports

- `codex-cli`: isolated `codex exec --ephemeral --ignore-user-config --ignore-rules`
- `agy`: fresh non-interactive `agy --print` calls
- `openai-compatible`: `/chat/completions` services
- `anthropic-compatible`: Anthropic Messages-compatible `/v1/messages` services

Each stage may use a different transport, or all stages may share one model. API
keys are named by `api_key_env` in YAML and supplied through environment variables.

## Quick start

```powershell
uv sync
uv run wenyi-direct init-config config.yaml
# edit config.yaml and set the referenced API-key environment variable if needed
uv run wenyi-direct translate path\to\book.epub --config config.yaml
uv run wenyi-direct status path\to\book.epub --config config.yaml
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

## Provenance

Document ingestion/assembly, resumable storage, and the Codex/Agy/OpenAI-compatible
adapter foundations are reused from Wenyi under the included MIT license. The
translation policy, prompts, repair planning, Chinese-only audit boundary,
Anthropic-compatible adapter, and command-line orchestration are new here.
