"""Command-line interface."""

from __future__ import annotations

from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from .assemble.writer import assemble as assemble_document
from .config import Config
from .llm.factory import build_clients
from .pipeline.direct import DirectPipeline, export_json
from .pipeline.runstore import STATUS_DONE, RunStore, slugify
from .validate import validate_epub

app = typer.Typer(no_args_is_help=True, help="Chapter-first literary translation.")
terms_app = typer.Typer(no_args_is_help=True, help="Manage human-confirmed hard terms.")
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
        chapter["index"]
        for chapter in manifest["chapters"]
        if chapter.get("status") != STATUS_DONE
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
    configured = cfg.hard_terms_file or "hard-terms.yaml"
    path = Path(configured)
    return path if path.is_absolute() else config_path.resolve().parent / path


@terms_app.command("list")
def list_terms(
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
) -> None:
    cfg = _load(config)
    path = _terms_path(cfg, config)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    for term in (data or {}).get("terms", []):
        console.print(
            f"{term.get('source')} -> {term.get('target')} "
            f"(from chapter {term.get('from_chapter', 0)})"
        )


@terms_app.command("add")
def add_term(
    source: str,
    target: str,
    from_chapter: int = typer.Option(0, min=0),
    note: str | None = typer.Option(None),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
) -> None:
    """Add or update a human-confirmed hard term."""
    cfg = _load(config)
    path = _terms_path(cfg, config)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    data = data or {}
    terms = list(data.get("terms", []) or [])
    terms = [term for term in terms if term.get("source") != source]
    terms.append(
        {
            "source": source,
            "target": target,
            "from_chapter": from_chapter,
            "note": note,
            "confirmed": True,
        }
    )
    data["terms"] = terms
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    console.print(f"Saved {source} -> {target} in {path}")
