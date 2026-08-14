# Unified model configuration

Kamyi separates reusable model connections from book-specific settings and
separates connection routes from models. A route is a user-defined way to call a
service or CLI, such as `agy`, `codex`, `web2api`, or `deepseek-api`. Each route has
its own user-defined model names, upstream model IDs, and request options. Default
stage routing selects both names. All of this lives in one user-level `models.yaml`.
Project YAML contains only language, windows, pipeline policy, state/output paths,
terminology, and optional exceptional role overrides.

```yaml
routes:
  deepseek-api:
    transport: openai-compatible
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    models:
      deepseek-v4-flash:
        model: deepseek-v4-flash

roles:
  translate: {route: deepseek-api, model: deepseek-v4-flash}
```

The keys `deepseek-api` and `deepseek-v4-flash` are aliases, not hard-coded product
names. Users may rename either one. The nested `model` value is the upstream ID sent
to the selected route.

## Central path and precedence

The default catalog is `%APPDATA%/kamyi/models.yaml` on Windows and
`$XDG_CONFIG_HOME/kamyi/models.yaml` (or `~/.config/kamyi/models.yaml`)
elsewhere. `KAMYI_MODELS` (with legacy `WENYI_DIRECT_MODELS` support) or the
model-management command's `--models` option
may select another catalog for portable/test environments.

When the user catalog exists, it is authoritative and is not merged with hidden
built-in entries. The built-in catalog is used only when no user catalog exists and
is what `models init` writes. Resolution order is:

1. the complete user `models.yaml` catalog, or the built-in catalog when absent;
2. optional project `roles` entries, each containing both `route` and `model`;
3. repeatable one-run `--model ROLE=ROUTE/MODEL` CLI overrides.

Only the `routes -> models` catalog schema is accepted. Project YAML cannot define
connections, and old `providers`, string-only role values, short model references,
and implicit schema migration are rejected rather than interpreted.

## Commands

```powershell
uv run kamyi models path
uv run kamyi models list
uv run kamyi use translate deepseek-api deepseek-v4-flash
uv run kamyi use audit web2api gemini-3.1-pro
uv run kamyi use repair codex gpt-5.6-sol-high

# One run only; does not edit either YAML file.
uv run kamyi review book.epub --model audit=web2api/gemini-3.1-pro --model repair=codex/gpt-5.6-sol-high
```

`audit` changes both `factual_audit` and `chinese_audit`. Individual logical roles,
`fallback`, and `all` are also accepted. The pipeline still owns the information
boundaries: switching a route/model changes routing only, never which prompt/context
that role may see.

The central writer saves atomically and stores only API-key environment-variable
names, never key values. `init-config` and `models init` are non-destructive.
Materialized runtime identities are deterministically `ROUTE::MODEL`; there is no
third alias layer.
