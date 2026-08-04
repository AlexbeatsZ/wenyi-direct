# Provider configuration

Logical roles (`translate`, `factual_audit`, `chinese_audit`, `repair`, and
`validation`) refer to named providers. Multiple roles may share one constructed
client, or use different transports.

An optional `roles.content_policy_fallback` names one provider used only when the
selected stage provider raises an explicit content-policy refusal. Quota errors,
timeouts, malformed JSON, alignment failures, and ordinary runtime errors do not
switch providers.

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
    cwd: C:/path/to/dedicated/agy-runtime
    isolate_user_config: true
    timeout: 1200
    tiers:
      strong:
        model: gemini-3.1-pro-high
```

The adapter writes the complete business prompt to a per-call UTF-8 temporary TXT
file. Only a short instruction and file name enter `--print`, so full-chapter prompts
cannot exceed Windows' command-line limit. Each call uses a fresh temporary workspace,
`--new-project`, sandbox restrictions, disabled slash expansion, and serialized
process access. `isolate_user_config: true` additionally requires a dedicated `cwd`
and gives the child isolated HOME/USERPROFILE state; it is recommended for every Agy
provider and is mechanically exercised by the provider tests.

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
