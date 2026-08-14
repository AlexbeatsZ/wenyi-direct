# Provider configuration

Logical roles (`translate`, `factual_audit`, `chinese_audit`, `repair`, and
`validation`) select a named route plus a named model available on that route.
Routes own transport/connection settings; models own the upstream model ID and its
request options. Both aliases are user-defined in the catalog printed by
`wenyi-direct models path`. Project YAML may still define legacy providers as a
backward-compatible, self-contained override.

Use `wenyi-direct models list` to inspect effective profiles,
`wenyi-direct use ROLE ROUTE MODEL` to persist a default mapping, or repeat
`--model ROLE=ROUTE/MODEL` on `translate`, `review`, and `stage` for one run only. See
[design/model-configuration.md](design/model-configuration.md) for precedence.

An optional `roles.content_policy_fallback` names one provider used when the
selected stage provider raises an explicit content-policy refusal, or after a CLI
provider exhausts `max_retries` for a recognized transient transport/runtime
failure such as EOF, connection reset, rate limiting, service unavailability, or
timeout. Every explicit timeout report, including a headless authentication timeout,
is retried up to that bound. Repeated invalid JSON also routes to the fallback after
the primary's own JSON retries are exhausted. Authentication failures without an
explicit timeout, configuration, alignment, and other permanent or quality-gate
failures do not switch providers.

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
`--new-project`, sandbox restrictions, and disabled slash expansion. Calls may
overlap because their workspaces, request files, and new-project sessions are
independent; this remains true when all roles share one configured provider.
`isolate_user_config: true` additionally requires a dedicated `cwd`
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
