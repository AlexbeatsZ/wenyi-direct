# Kamyi

Kamyi is a chapter-first literary translation pipeline for EPUB, FB2, TXT,
Markdown, HTML, PDF, and the documented JSON interchange format. It translates a
chapter directly, audits factual fidelity, reviews the Chinese reading experience,
and promotes only validated text to the final output.

Inspired by [Wenyi](https://github.com/BigDawnGhost/wenyi), this repository contains
the reusable translator, tests, design documents, and
sanitized configuration examples. It does not contain generated book translations,
runtime state, private source text, or API credentials.

## What the pipeline does

For each chapter, Kamyi:

1. translates the source into a resumable Shadow chapter;
2. runs a source-aware factual audit and repairs validated causal regions;
3. runs a Chinese-reader audit using Chinese text only, then validates any repair
   against the source;
4. atomically promotes the reviewed chapter to Formal text.

The pipeline is chapter-first. Future chapters are never required, and model calls
cannot write segment IDs that were not explicitly authorized for that stage.

## Install

Windows PowerShell:

```powershell
git clone https://github.com/AlexbeatsZ/kamyi.git
cd kamyi
uv sync
uv run kamyi --help
```

`uv` creates and uses the project environment. No global Python package or system
configuration is required.

## Configure a project

Create a book-specific configuration and the user-level model catalog:

```powershell
uv run kamyi init-config config.yaml
uv run kamyi models path
uv run kamyi models list
```

`config.yaml` contains language, context-window, pipeline, output, and state
settings for a book. The reusable provider connections and model choices belong in
the catalog printed by `models path` (normally
`%APPDATA%\kamyi\models.yaml` on Windows).

Start from the checked-in examples when needed:

```powershell
Copy-Item config.example.yaml config.yaml
New-Item -ItemType Directory -Force "$env:APPDATA\kamyi" | Out-Null
Copy-Item models.example.yaml "$env:APPDATA\kamyi\models.yaml"
```

The command `init-config` is non-destructive and will not overwrite existing files.
Do not commit `config.yaml`, `models.yaml`, book state, or generated output.

## API keys and provider setup

API keys are never stored in project YAML, source code, Git, or README examples.
Provider configuration stores only the name of an environment variable, for example
`api_key_env: DEEPSEEK_API_KEY`.

Set the variable in the shell that runs Kamyi, then configure the matching
route in the user catalog:

```powershell
$env:DEEPSEEK_API_KEY = 'replace-with-your-key'
uv run kamyi models list
uv run kamyi use translate deepseek-api deepseek-v4-flash
```

Never paste a real key into `config.yaml`, `models.yaml`, a command line, a test,
or a commit. `codex-cli` and `agy` routes use their own local CLI authentication;
the adapters run business calls in isolated, read-only processes. See
[docs/providers.md](docs/providers.md) for OpenAI-compatible, Anthropic-compatible,
Codex CLI, and Agy route examples.

## Translate a book

The normal command is resumable. Running it again continues pending chapters and
does not repeat completed stages:

```powershell
uv run kamyi translate path\to\book.epub --config config.yaml
```

To overlap downstream review for chapter N with upstream work for chapter N+1:

```powershell
uv run kamyi translate path\to\book.epub --parallel --config config.yaml
```

Limit a run to selected chapter indexes or ranges with `--chapters`, for example
`--chapters 0,2-4`. A one-run model override does not edit either configuration:

```powershell
uv run kamyi translate path\to\book.epub `
  --model translate=deepseek-api/deepseek-v4-flash `
  --model repair=codex/gpt-5.6-sol-high `
  --config config.yaml
```

## Review existing Formal text

`review` starts a fresh, resumable factual and Chinese-reader review from existing
Formal chapters without translating them again:

```powershell
uv run kamyi review path\to\book.epub --parallel --config config.yaml
```

Use `--sequential` when provider concurrency is unsuitable. Completed review
generations require `--force` before they can be reopened.

## Inspect, monitor, and assemble

```powershell
# No model call: show persisted chapter/stage progress.
uv run kamyi status path\to\book.epub --config config.yaml

# Read-only local monitor for stages, Formal, Shadow, and audit events.
uv run kamyi monitor path\to\book.epub --config config.yaml --port 8765

# Export only atomically promoted Formal text.
uv run kamyi assemble path\to\book.epub --config config.yaml --format epub
```

By default, resumable state is stored under `state/` and assembled files under
`outputs/`; both are intentionally ignored by Git. `Shadow` candidates are never
included by `assemble`.

## Independent stages and terminology

Stages can be run independently when their persisted prerequisites exist:

```powershell
uv run kamyi stage translate path\to\book.epub --chapters 0-3 --config config.yaml
uv run kamyi stage factual-audit path\to\book.epub --chapters 0-3 --config config.yaml
uv run kamyi stage chinese-audit path\to\book.epub --chapters 0-3 --config config.yaml
uv run kamyi stage promote path\to\book.epub --chapters 0-3 --config config.yaml
```

Terminology rules are book-local and can be managed without editing generated
state by hand:

```powershell
uv run kamyi terms list --config config.yaml
uv run kamyi terms group-add flame 炎 火焰 --config config.yaml
uv run kamyi terms add 炎魔法 火焰魔法 --group flame --mode hard --config config.yaml
uv run kamyi terms set-status 炎魔法 active --config config.yaml
```

## Development checks

```powershell
uv run ruff check .
uv run pytest
```

Architecture and boundary details are in
[docs/architecture.md](docs/architecture.md),
[docs/design/stage-execution.md](docs/design/stage-execution.md),
[docs/design/model-configuration.md](docs/design/model-configuration.md),
[docs/formats.md](docs/formats.md), and [docs/providers.md](docs/providers.md).

## Provenance and license

Document ingestion/assembly, resumable storage, and provider adapter foundations
are reused from [Wenyi](https://github.com/BigDawnGhost/wenyi) under the included
MIT license. The translation policy, prompts, repair planning, Chinese-only audit
boundary, and chapter-first orchestration are Kamyi additions inspired by Wenyi.
