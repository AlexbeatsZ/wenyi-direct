# Unified model configuration

Wenyi Direct separates reusable model connections from book-specific settings.
Provider transports, model IDs, reasoning options, API-key environment-variable
names, and default stage routing live in one user-level `models.yaml`. Project YAML
contains only language, windows, pipeline policy, state/output paths, terminology,
and optional exceptional role overrides.

## Central path and precedence

The default catalog is `%APPDATA%/wenyi-direct/models.yaml` on Windows and
`$XDG_CONFIG_HOME/wenyi-direct/models.yaml` (or `~/.config/wenyi-direct/models.yaml`)
elsewhere. `WENYI_DIRECT_MODELS` or the model-management command's `--models` option
may select another catalog for portable/test environments.

Resolution order is:

1. built-in safe profiles;
2. the user `models.yaml` provider definitions and default roles;
3. optional project `providers`/`roles` for backward compatibility or an explicit
   project exception;
4. repeatable one-run `--model ROLE=MODEL` CLI overrides.

A legacy project file containing its own `providers` remains self-contained: absent
roles retain the historical `default` mapping instead of unexpectedly inheriting a
new global role selection. A sparse project file with no `providers` inherits the
central catalog and roles.

## Commands

```powershell
uv run wenyi-direct models path
uv run wenyi-direct models list
uv run wenyi-direct models use audit deepseek_pro
uv run wenyi-direct models use repair codex_sol

# One run only; does not edit either YAML file.
uv run wenyi-direct review book.epub --model audit=gemini_pro --model repair=codex_sol
```

`audit` changes both `factual_audit` and `chinese_audit`. Individual logical roles,
`fallback`, and `all` are also accepted. The pipeline still owns the information
boundaries: switching a model changes routing only, never which prompt/context that
role may see.

The central writer saves atomically and stores only API-key environment-variable
names, never key values. `init-config` and `models init` are non-destructive.
