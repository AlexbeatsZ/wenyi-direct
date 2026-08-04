from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from wenyi_direct.cli import app
from wenyi_direct.ingest.models import Chapter, Segment
from wenyi_direct.pipeline.knowledge import TerminologyDocument, TerminologyStore, TermRule
from wenyi_direct.pipeline.runstore import RunStore

runner = CliRunner()


def _setup(tmp_path: Path, *, target: str) -> tuple[Path, Path, Path]:
    book = tmp_path / "book.json"
    book.write_text(
        json.dumps(
            {
                "title": "book",
                "chapters": [
                    {
                        "title": "chapter",
                        "segments": [{"source": "黒炎を放った。"}],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    state_root = tmp_path / "state"
    store = RunStore(str(state_root / "book"))
    store.save_chapter(
        Chapter(
            index=0,
            title="chapter",
            segments=[Segment(index=0, source="黒炎を放った。", target=target)],
        )
    )
    store.save_manifest(
        {
            "title": "book",
            "source_path": str(book),
            "chapters": [{"index": 0, "title": "chapter", "status": "done"}],
        }
    )
    terminology = TerminologyStore(
        Path(store.run_dir) / "terminology.yaml",
        TerminologyDocument(
            terms=[
                TermRule(
                    source="黒炎",
                    target="黑色火焰",
                    mode="hard",
                    status="active",
                )
            ]
        ),
    )
    terminology.save()
    config = tmp_path / "config.yaml"
    config.write_text(
        """language:
  source: ja
  target: zh-CN
providers:
  default:
    provider: fake
roles:
  translate: default
  factual_audit: default
  chinese_audit: default
  repair: default
  validation: default
paths:
  state_dir: state
  output_dir: outputs
  terminology_file: terminology.yaml
""",
        encoding="utf-8",
    )
    return book, config, Path(store.run_dir)


def test_terms_revise_no_model_reports_ambiguity_without_changes(tmp_path: Path) -> None:
    book, config, run_dir = _setup(tmp_path, target="释放了漆黑烈焰。")

    result = runner.invoke(
        app,
        [
            "terms",
            "revise",
            str(book),
            "--source",
            "黒炎",
            "--old-target",
            "黑色火焰",
            "--new-target",
            "黑炎",
            "--no-model",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 2
    assert "Ambiguous terminology uses" in result.output
    assert RunStore(str(run_dir)).load_chapter(0).segments[0].target == "释放了漆黑烈焰。"
    terminology = TerminologyStore.load(run_dir / "terminology.yaml")
    assert terminology.find_active_rule("黒炎", "黑色火焰").target == "黑色火焰"


def test_terms_revise_safe_migration_does_not_build_model_clients(tmp_path: Path) -> None:
    book, config, run_dir = _setup(tmp_path, target="释放了黑色火焰。")

    result = runner.invoke(
        app,
        [
            "terms",
            "revise",
            str(book),
            "--source",
            "黒炎",
            "--old-target",
            "黑色火焰",
            "--new-target",
            "黑炎",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "model_resolved=0" in result.output
    assert RunStore(str(run_dir)).load_chapter(0).segments[0].target == "释放了黑炎。"
    terminology = TerminologyStore.load(run_dir / "terminology.yaml")
    assert terminology.find_active_rule("黒炎", "黑炎").target == "黑炎"
