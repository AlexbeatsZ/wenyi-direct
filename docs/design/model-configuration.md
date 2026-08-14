# Unified model configuration

Wenyi Direct separates reusable model connections from book-specific settings and
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

The default catalog is `%APPDATA%/wenyi-direct/models.yaml` on Windows and
`$XDG_CONFIG_HOME/wenyi-direct/models.yaml` (or `~/.config/wenyi-direct/models.yaml`)
elsewhere. `WENYI_DIRECT_MODELS` or the model-management command's `--models` option
may select another catalog for portable/test environments.

Resolution order is:

1. built-in safe routes/models;
2. the user `models.yaml` route definitions, model aliases, and default selections;
3. optional project `providers`/`roles` for backward compatibility or an explicit
   project exception;
4. repeatable one-run `--model ROLE=ROUTE/MODEL` CLI overrides.

A legacy project file containing its own `providers` remains self-contained: absent
roles retain the historical `default` mapping instead of unexpectedly inheriting a
new global role selection. The first profile-based user-catalog schema is migrated
in memory. A sparse project file with no `providers` inherits the central routes,
models, and role selections.

## Commands

```powershell
uv run wenyi-direct models path
uv run wenyi-direct models list
uv run wenyi-direct use translate deepseek-api deepseek-v4-flash
uv run wenyi-direct use audit web2api gemini-3.1-pro
uv run wenyi-direct use repair codex gpt-5.6-sol-high

# One run only; does not edit either YAML file.
uv run wenyi-direct review book.epub --model audit=web2api/gemini-3.1-pro --model repair=codex/gpt-5.6-sol-high
```

`audit` changes both `factual_audit` and `chinese_audit`. Individual logical roles,
`fallback`, and `all` are also accepted. `models use` is a compatibility alias for
the top-level `use` command. The pipeline still owns the information boundaries:
switching a route/model changes routing only, never which prompt/context that role
may see.

The central writer saves atomically and stores only API-key environment-variable
names, never key values. `init-config` and `models init` are non-destructive. Hidden
`runtime_name` fields exist only for additive migration of active resumable work;
ordinary route/model aliases do not need them.
