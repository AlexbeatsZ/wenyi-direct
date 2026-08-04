"""CLI groups for granular tasks and staggered execution."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from .config import Config
from .llm.factory import build_clients
from .pipeline.tasks import StageTaskError, TaskPipeline

stage_app = typer.Typer(
    no_args_is_help=True,
    help="Run one pipeline task without automatically continuing into later tasks.",
)
pipeline_app = typer.Typer(
    no_args_is_help=True,
    help="Run alternative multi-chapter scheduling modes.",
)
console = Console()


def _parse_chapters(value: str | None) -> set[int] | None:
    if not value:
        return None
    result: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            start = int(left)
            end = int(right)
            if end < start:
                raise typer.BadParameter(f"invalid chapter range {part!r}")
            result.update(range(start, end + 1))
        else:
            result.add(int(part))
    return result


def _pipeline(config_path: Path) -> TaskPipeline:
    config = Config.load(config_path)
    return TaskPipeline(
        config,
        build_clients(config),
        config_dir=config_path.resolve().parent,
    )


def _print_result(store, task: str) -> None:
    manifest = store.load_manifest()
    console.print(f"Completed task: {task}")
    for chapter in manifest["chapters"]:
        console.print(
            f"ch{chapter['index']}: status={chapter.get('status', 'pending')} "
            f"phase={chapter.get('phase', 'not_started')} "
            f"next={chapter.get('task', '')}"
        )


def _run_stage(
    task: str,
    source: Path,
    config: Path,
    chapters: str | None,
) -> None:
    pipeline = _pipeline(config)
    try:
        store = pipeline.run_stage(
            source,
            task,
            chapters=_parse_chapters(chapters),
        )
    except (StageTaskError, RuntimeError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    _print_result(store, task)


_COMMON_SOURCE = typer.Argument(..., exists=True, dir_okay=False)
_COMMON_CONFIG = typer.Option(Path("config.yaml"), "--config", "-c", exists=True)
_COMMON_CHAPTERS = typer.Option(
    None,
    "--chapters",
    help="Optional indexes/ranges, e.g. 0,2-4.",
)


@stage_app.command("translate")
def stage_translate(
    source: Path = _COMMON_SOURCE,
    config: Path = _COMMON_CONFIG,
    chapters: str | None = _COMMON_CHAPTERS,
) -> None:
    """Run direct translation only, stopping before factual review."""
    _run_stage("translate", source, config, chapters)


@stage_app.command("factual-audit")
def stage_factual_audit(
    source: Path = _COMMON_SOURCE,
    config: Path = _COMMON_CONFIG,
    chapters: str | None = _COMMON_CHAPTERS,
) -> None:
    """Find factual/source-faithfulness issues without repairing them."""
    _run_stage("factual-audit", source, config, chapters)


@stage_app.command("factual-repair")
def stage_factual_repair(
    source: Path = _COMMON_SOURCE,
    config: Path = _COMMON_CONFIG,
    chapters: str | None = _COMMON_CHAPTERS,
) -> None:
    """Repair previously audited factual issues and validate the result."""
    _run_stage("factual-repair", source, config, chapters)


@stage_app.command("chinese-audit")
def stage_chinese_audit(
    source: Path = _COMMON_SOURCE,
    config: Path = _COMMON_CONFIG,
    chapters: str | None = _COMMON_CHAPTERS,
) -> None:
    """Run Chinese-only fluency review and source-aware finding validation."""
    _run_stage("chinese-audit", source, config, chapters)


@stage_app.command("chinese-repair")
def stage_chinese_repair(
    source: Path = _COMMON_SOURCE,
    config: Path = _COMMON_CONFIG,
    chapters: str | None = _COMMON_CHAPTERS,
) -> None:
    """Repair validated Chinese-reading issues, including bounded recheck."""
    _run_stage("chinese-repair", source, config, chapters)


@stage_app.command("promote")
def stage_promote(
    source: Path = _COMMON_SOURCE,
    config: Path = _COMMON_CONFIG,
    chapters: str | None = _COMMON_CHAPTERS,
) -> None:
    """Atomically promote a completed Shadow candidate into Formal text."""
    _run_stage("promote", source, config, chapters)


@pipeline_app.command("fast")
def pipeline_fast(
    source: Path = _COMMON_SOURCE,
    config: Path = _COMMON_CONFIG,
    chapters: str | None = _COMMON_CHAPTERS,
) -> None:
    """Overlap chapter N Chinese work with chapter N+1 factual work."""
    pipeline = _pipeline(config)
    try:
        store = pipeline.run_fast(source, chapters=_parse_chapters(chapters))
    except (StageTaskError, RuntimeError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    _print_result(store, "pipeline fast")


__all__ = ["pipeline_app", "stage_app"]
