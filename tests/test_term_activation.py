from __future__ import annotations

import json
from pathlib import Path

from wenyi_direct.ingest.models import Chapter, Segment
from wenyi_direct.pipeline.knowledge import TerminologyDocument, TerminologyStore, TermRule
from wenyi_direct.prompts import (
    factual_audit_messages,
    fidelity_validation_messages,
    translation_messages,
)


def _write_run_state(run_dir: Path, *, status: str, target: str) -> None:
    (run_dir / "chapters").mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "chapters": [
                    {"index": 0, "title": "chapter", "status": status}
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "chapters" / "ch0.json").write_text(
        json.dumps(
            {
                "index": 0,
                "title": "chapter",
                "segments": [
                    {
                        "index": 0,
                        "source": "ノエルが来た。",
                        "target": target,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_discovered_term_stays_candidate_until_formal_confirmation(tmp_path: Path) -> None:
    run_dir = tmp_path / "state" / "book"
    _write_run_state(run_dir, status="pending", target="诺艾尔来了。")
    store = TerminologyStore(run_dir / "terminology.yaml")

    added = store.add_discoveries(
        0,
        [
            {
                "segment_id": "ch0:s0",
                "source": "ノエル",
                "target": "诺艾尔",
            }
        ],
        "ノエルが来た。",
        "诺艾尔来了。",
    )

    assert len(added) == 1
    assert added[0].status == "candidate"
    assert store.visible_for_translation(1, "ノエル") == {
        "hard_terms": [],
        "preferred_terms": [],
    }

    _write_run_state(run_dir, status="done", target="诺艾尔来了。")
    terms = store.terms

    assert terms[0].status == "active"
    visible = store.visible_for_translation(1, "ノエル")
    assert visible["preferred_terms"] == [
        {"source": "ノエル", "target": "诺艾尔"}
    ]


def test_discovered_term_is_not_activated_when_final_text_changed(tmp_path: Path) -> None:
    run_dir = tmp_path / "state" / "book"
    _write_run_state(run_dir, status="pending", target="诺艾尔来了。")
    store = TerminologyStore(run_dir / "terminology.yaml")
    store.add_discoveries(
        0,
        [
            {
                "segment_id": "ch0:s0",
                "source": "ノエル",
                "target": "诺艾尔",
            }
        ],
        "ノエルが来た。",
        "诺艾尔来了。",
    )

    _write_run_state(run_dir, status="done", target="诺埃尔来了。")

    assert store.terms[0].status == "candidate"
    assert store.visible_for_translation(1, "ノエル")["preferred_terms"] == []


def test_auditors_receive_challengeable_terminology_projection() -> None:
    chapter = Chapter(
        index=0,
        title="chapter",
        segments=[Segment(index=0, source="黒炎。", target="黑色火焰。")],
    )
    knowledge = {
        "hard_terms": [{"source": "黒炎", "target": "黑色火焰"}],
        "preferred_terms": [{"source": "別名", "target": "别名"}],
    }

    translation_payload = json.loads(
        translation_messages(chapter, (0,), (0,), knowledge)[-1]["content"].split("\n", 1)[1]
    )
    audit_payload = json.loads(
        factual_audit_messages(chapter, (0,), (0,), {0: "黑色火焰。"}, knowledge)[-1][
            "content"
        ].split("\n", 1)[1]
    )
    fidelity_payload = json.loads(
        fidelity_validation_messages(chapter, {0: "黑炎。"}, (0,), (0,), knowledge)[-1][
            "content"
        ].split("\n", 1)[1]
    )

    assert translation_payload["knowledge"]["hard_terms"] == [
        {"source": "黒炎", "target": "黑色火焰"}
    ]
    for payload in (audit_payload, fidelity_payload):
        projected = payload["knowledge"]
        assert "hard_terms" not in projected
        assert "preferred_terms" not in projected
        assert projected["current_terminology"] == [
            {
                "source": "黒炎",
                "current_target": "黑色火焰",
                "current_mode": "hard",
                "current_status": "active",
                "challengeable": True,
            },
            {
                "source": "別名",
                "current_target": "别名",
                "current_mode": "preferred",
                "current_status": "active",
                "challengeable": True,
            },
        ]


def test_manual_candidate_is_never_auto_promoted(tmp_path: Path) -> None:
    run_dir = tmp_path / "state" / "book"
    _write_run_state(run_dir, status="done", target="诺艾尔来了。")
    store = TerminologyStore(
        run_dir / "terminology.yaml",
        TerminologyDocument(
            terms=[
                TermRule(
                    source="ノエル",
                    target="诺艾尔",
                    mode="preferred",
                    status="candidate",
                )
            ]
        ),
    )
    store.save()

    assert store.terms[0].status == "candidate"
