# Provider configuration

Logical roles (`translate`, `factual_audit`, `chinese_audit`, `repair`, and
`validation`) refer to named providers. Multiple roles may share one constructed
client, or use different transports.

## Codex CLI

```yaml
providers:
  sol:
    provider: codex-cli
    command: codex
    cwd: C:/an/existing/read-only-working-directory
    timeout: 1200
    tiers:
      strong:
        model: gpt-5.6-sol
        options: {reasoning_effort: high}
```

Every request uses an ephemeral `codex exec` process with user configuration and
rules ignored, repository checks skipped, tools disabled by a read-only sandbox, and
the complete business prompt on stdin.

## Agy CLI

```yaml
providers:
  gemini:
    provider: agy
    command: agy
    timeout: 1200
    tiers:
      strong:
        model: gemini-3.1-pro-high
```

The adapter uses a fresh non-interactive `--print` request and serializes processes
to avoid contention in Agy's local state. `isolate_user_config: true` additionally
requires a dedicated `cwd` and gives the child isolated HOME/USERPROFILE state.

## OpenAI-compatible Chat Completions

```yaml
providers:
  api:
    provider: openai-compatible
    base_url: https://example.invalid/v1
    api_key_env: TRANSLATION_API_KEY
    reasoning_style: none
    tiers:
      strong:
        model: vendor-model-name
        options:
          request_overrides:
            temperature: 0.2
```

## Anthropic-compatible Messages

```yaml
providers:
  messages_api:
    provider: anthropic-compatible
    base_url: https://api.anthropic.com
    api_key_env: ANTHROPIC_API_KEY
    tiers:
      strong:
        model: claude-sonnet-4-5
        options:
          max_tokens: 12000
```

The endpoint may be a server root, `/v1`, or the full `/v1/messages` URL. System
messages are sent through the native `system` field. Unknown vendor fields may be
placed under `request_overrides` inside tier options.
