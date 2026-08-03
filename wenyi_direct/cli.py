"""Command-line interface."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .assemble.writer import assemble as assemble_document
from .config import Config
from .llm.factory import build_clients
from .pipeline.direct import DirectPipeline, export_json
from .pipeline.knowledge import TerminologyStore, TermRule
from .pipeline.runstore import STATUS_DONE, RunStore, slugify
from .validate import validate_epub

app = typer.Typer(no_args_is_help=True, help="Chapter-first literary translation.")
terms_app = typer.Typer(no_args_is_help=True, help="Manage terminology rules and groups.")
app.add_typer(terms_app, name="terms")
console = Console()


def _load(config_path: Path) -> Config:
    return Config.load(config_path)


def _store(config: Config, config_path: Path, source: Path) -> RunStore:
    root = Path(config.state_dir)
    if not root.is_absolute():
        root = config_path.resolve().parent / root
    return RunStore(str(root / slugify(source.stem)), create=False)


def _parse_chapters(value: str | None) -> set[int] | None:
    if not value:
        return None
    result: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            left, right = part.split("-", 1)
            result.update(range(int(left), int(right) + 1))
        else:
            result.add(int(part))
    return result


@app.command("init-config")
def init_config(path: Path = typer.Argument(Path("config.yaml"))) -> None:
    """Create a documented default configuration without overwriting an existing file."""
    if Config.create_default_file(path):
        console.print(f"Created {path}")
    else:
        console.print(f"Already exists: {path}")


@app.command()
def prepare(
    source: Path = typer.Argument(..., exists=True, dir_okay=False),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
) -> None:
    """Parse the source and create resumable state without calling a model."""
    cfg = _load(config)
    pipeline = DirectPipeline(cfg, {}, config_dir=config.resolve().parent)
    store = pipeline.prepare(source)
    console.print(store.run_dir)


@app.command()
def translate(
    source: Path = typer.Argument(..., exists=True, dir_okay=False),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
    chapters: str | None = typer.Option(
        None, help="Optional indexes/ranges, e.g. 0,2-4. Default resumes all pending chapters."
    ),
) -> None:
    """Resume direct translation and all configured quality gates."""
    cfg = _load(config)
    clients = build_clients(cfg)
    pipeline = DirectPipeline(cfg, clients, config_dir=config.resolve().parent)
    store = pipeline.run(source, chapters=_parse_chapters(chapters))
    _print_status(store)


@app.command()
def status(
    source: Path = typer.Argument(..., exists=True, dir_okay=False),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
) -> None:
    """Show persisted progress without calling any model."""
    cfg = _load(config)
    store = _store(cfg, config, source)
    if not store.exists():
        raise typer.BadParameter("no state exists for this source; run prepare or translate")
    _print_status(store)


@app.command()
def monitor(
    source: Path = typer.Argument(..., exists=True, dir_okay=False),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
    host: str = typer.Option("127.0.0.1", help="Read-only monitor bind address."),
    port: int = typer.Option(8765, min=0, max=65535),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """Serve a live audit trail and Formal/Shadow chapter reader."""
    from .monitor import serve

    cfg = _load(config)
    store = _store(cfg, config, source)
    if not store.exists():
        raise typer.BadParameter("no state exists for this source; run prepare or translate")
    serve(
        Path(store.run_dir),
        config,
        host=host,
        port=port,
        open_browser=open_browser,
    )


def _print_status(store: RunStore) -> None:
    manifest = store.load_manifest()
    table = Table(title=manifest["title"])
    table.add_column("Chapter", justify="right")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Phase")
    table.add_column("Error")
    for chapter in manifest["chapters"]:
        table.add_row(
            str(chapter["index"]),
            str(chapter.get("title", "")),
            str(chapter.get("status", "pending")),
            str(chapter.get("phase", "not_started")),
            str(chapter.get("error") or ""),
        )
    console.print(table)
    done = sum(chapter.get("status") == STATUS_DONE for chapter in manifest["chapters"])
    console.print(f"{done}/{len(manifest['chapters'])} formal chapters complete")


@app.command()
def assemble(
    source: Path = typer.Argument(..., exists=True, dir_okay=False),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
    format: str = typer.Option("epub", "--format", "-f"),
    output: Path | None = typer.Option(None, "--output", "-o"),
    bilingual: bool | None = typer.Option(None, "--bilingual/--mono"),
) -> None:
    """Assemble formal text only; shadow candidates are never visible here."""
    cfg = _load(config)
    store = _store(cfg, config, source)
    if not store.exists():
        raise typer.BadParameter("no state exists for this source")
    manifest = store.load_manifest()
    incomplete = [
        chapter["index"] for chapter in manifest["chapters"] if chapter.get("status") != STATUS_DONE
    ]
    if incomplete:
        raise typer.BadParameter(f"formal translation is incomplete: chapters {incomplete}")
    out_root = Path(cfg.output_dir)
    if not out_root.is_absolute():
        out_root = config.resolve().parent / out_root
    extension = {"markdown": "md"}.get(format, format)
    output = output or (out_root / f"{source.stem}.zh.{extension}")
    if format == "json":
        result = export_json(store, output)
    else:
        result = assemble_document(
            store,
            str(source.resolve()),
            str(output.resolve()),
            out_format=format,
            bilingual=cfg.output.bilingual if bilingual is None else bilingual,
            order=cfg.output.bilingual_order,
            preserve_source_style=cfg.output.bilingual_preserve_source_style,
            about_page=cfg.output.about_page,
        )
    console.print(result)
    if format == "epub":
        console.print(validate_epub(result))


def _terms_path(cfg: Config, config_path: Path) -> Path:
    configured = cfg.terminology_file or "terminology.yaml"
    path = Path(configured)
    return path if path.is_absolute() else config_path.resolve().parent / path


@terms_app.command("list")
def list_terms(
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
) -> None:
    cfg = _load(config)
    store = TerminologyStore.load(_terms_path(cfg, config))
    table = Table(title="Terminology")
    for column in ("Source", "Target", "Group", "Mode", "Status", "Range", "Pronoun"):
        table.add_column(column)
    for term in store.terms:
        valid_range = f"{term.valid_from if term.valid_from is not None else '*'}..{term.valid_to if term.valid_to is not None else '*'}"
        table.add_row(
            term.source,
            term.target,
            term.group_id or "",
            term.mode,
            term.status,
            valid_range,
            term.pronoun or "",
        )
    console.print(table)


@terms_app.command("add")
def add_term(
    source: str,
    target: str,
    group_id: str | None = typer.Option(None, "--group"),
    mode: str = typer.Option("hard", help="hard or preferred"),
    status: str = typer.Option("active", help="active, candidate, or rejected"),
    valid_from: int | None = typer.Option(None, min=0),
    valid_to: int | None = typer.Option(None, min=0),
    pronoun: str | None = typer.Option(None, help="他, 她, 它, or neutral"),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
) -> None:
    """Add or replace the rule with the same source and chapter range."""
    cfg = _load(config)
    store = TerminologyStore.load(_terms_path(cfg, config))
    store.add_term(
        TermRule(
            source=source,
            target=target,
            group_id=group_id,
            mode=mode,
            status=status,
            valid_from=valid_from,
            valid_to=valid_to,
            pronoun=pronoun,
        )
    )
    console.print(f"Saved {source} -> {target} in {store.path}")


@terms_app.command("set-status")
def set_term_status(
    source: str,
    status: str,
    target: str | None = typer.Option(
        None, "--target", help="Only change the matching target translation"
    ),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
) -> None:
    """Change matching rules to active, candidate, or rejected."""
    cfg = _load(config)
    if status not in {"active", "candidate", "rejected"}:
        raise typer.BadParameter("status must be active, candidate, or rejected")
    store = TerminologyStore.load(_terms_path(cfg, config))
    changed = store.set_status(source, status, target=target)
    console.print(f"Updated {changed} rule(s)")


@terms_app.command("group-add")
def add_group(
    group_id: str,
    source_anchor: str,
    target_anchor: str,
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
) -> None:
    """Add or replace one translation-sharing group."""
    cfg = _load(config)
    store = TerminologyStore.load(_terms_path(cfg, config))
    store.add_group(group_id, source_anchor, target_anchor)
    console.print(f"Saved group {group_id} in {store.path}")


@terms_app.command("group-list")
def list_groups(
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
) -> None:
    cfg = _load(config)
    store = TerminologyStore.load(_terms_path(cfg, config))
    table = Table(title="Terminology groups")
    table.add_column("ID")
    table.add_column("Source anchor")
    table.add_column("Target anchor")
    for group_id, group in store.groups.items():
        table.add_row(group_id, group.source_anchor, group.target_anchor)
    console.print(table)
