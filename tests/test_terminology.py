from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from wenyi_direct.llm.providers.fake import FakeClient
from wenyi_direct.pipeline.direct import DirectPipeline
from wenyi_direct.pipeline.knowledge import TerminologyStore, TermRule
from wenyi_direct.prompts import (
    FACTUAL_AUDIT_SYSTEM,
    FIDELITY_SYSTEM,
    REPAIR_SYSTEM,
    TRANSLATION_SYSTEM,
)

_CORE_PATH = Path(__file__).with_name("terminology_core_cases.py")
_SPEC = importlib.util.spec_from_file_location("wenyi_terminology_core_cases", _CORE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_CORE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _CORE
_SPEC.loader.exec_module(_CORE)

_REWRITTEN = {
    "test_discovery_auto_activates_soft_rule_but_conflict_becomes_candidate",
    "test_discovery_accepts_a_stable_ordinary_noun_phrase",
    "test_hard_term_is_repaired_even_when_model_audit_reports_no_issue",
    "test_failed_promotion_resumes_with_policy_invalidation_and_repairs",
    "test_discovery_is_bounded_before_a_scheduled_future_term",
}
for _name in dir(_CORE):
    if _name.startswith("test_") and _name not in _REWRITTEN:
        globals()[_name] = getattr(_CORE, _name)


def test_discovery_stays_candidate_until_formal_confirmation_or_manual_action(
    tmp_path: Path,
) -> None:
    store = TerminologyStore(tmp_path / "terminology.yaml")
    store.add_term(TermRule(source="ノエル", target="诺艾尔"))
    added = store.add_discoveries(
        2,
        [
            {"source": "ブラックホーク", "target": "黑鹰"},
            {"source": "ノエル", "target": "诺埃尔"},
        ],
        "ノエルとブラックホーク",
        "诺埃尔与黑鹰",
    )

    assert [(term.source, term.status, term.mode) for term in added] == [
        ("ブラックホーク", "candidate", "preferred"),
        ("ノエル", "candidate", "preferred"),
    ]
    assert store.visible(2, "ブラックホーク")["preferred_terms"] == []
    assert store.set_status("ノエル", "rejected", target="诺艾尔") == 1
    assert store.set_status("ノエル", "active", target="诺埃尔") == 1
    assert store.visible(2, "ノエル")["preferred_terms"][0]["target"] == "诺埃尔"


def test_discovery_accepts_stable_ordinary_noun_phrase_as_candidate(
    tmp_path: Path,
) -> None:
    store = TerminologyStore(tmp_path / "terminology.yaml")
    added = store.add_discoveries(
        3,
        [{"source": "焼き鳥屋", "target": "烤鸡串店"}],
        "今日も焼き鳥屋に寄った。",
        "今天也去了烤鸡串店。",
    )

    assert len(added) == 1
    assert added[0].source == "焼き鳥屋"
    assert added[0].target == "烤鸡串店"
    assert added[0].mode == "preferred"
    assert added[0].status == "candidate"
    assert added[0].valid_from == 3
    assert added[0].origin == "discovered"
    assert "普通名词短语" in FACTUAL_AUDIT_SYSTEM


def test_hard_term_is_repaired_while_fidelity_receives_challengeable_view(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.json"
    source.write_text(
        json.dumps(
            {
                "title": "test",
                "chapters": [{"title": "c0", "segments": [{"source": "光った。"}]}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    terminology = TerminologyStore(tmp_path / "terminology.yaml")
    terminology.add_term(TermRule(source="光った", target="亮了", mode="hard"))

    def handler(messages, _tier, _json_mode):
        system = messages[0]["content"]
        payload = json.loads(messages[-1]["content"].split("\n", 1)[1])
        if system == TRANSLATION_SYSTEM:
            stable_id = payload["required_output"]["translations"][0]["id"]
            return json.dumps(
                {"translations": [{"id": stable_id, "target": "闪光了。"}]},
                ensure_ascii=False,
            )
        if system == FACTUAL_AUDIT_SYSTEM:
            return json.dumps(
                {"issues": [], "term_candidates": [], "term_revisions": []}
            )
        if system == REPAIR_SYSTEM:
            assert payload["knowledge"]["hard_terms"][0]["target"] == "亮了"
            stable_id = payload["required_output"]["translations"][0]["id"]
            return json.dumps(
                {"translations": [{"id": stable_id, "target": "亮了。"}]},
                ensure_ascii=False,
            )
        if system == FIDELITY_SYSTEM:
            assert "hard_terms" not in payload["knowledge"]
            assert payload["knowledge"]["current_terminology"][0] == {
                "source": "光った",
                "current_target": "亮了",
                "current_mode": "hard",
                "current_status": "active",
                "challengeable": True,
            }
            return json.dumps({"valid": True, "issues": []})
        raise AssertionError(system)

    config = _CORE._minimal_config(tmp_path)
    fake = FakeClient(handler)
    pipeline = DirectPipeline(
        config, {role: fake for role in config.roles.model_dump()}, config_dir=tmp_path
    )
    store = pipeline.run(source)
    assert store.load_chapter(0).segments[0].target == "亮了。"


def test_failed_promotion_resumes_explicit_saved_phase_without_policy_hash(
    tmp_path: Path,
) -> None:
    source = tmp_path / "resume-book.json"
    source.write_text(
        json.dumps(
            {
                "title": "test",
                "chapters": [
                    {"title": "c0", "segments": [{"source": "ブラックホーク"}]}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    terminology = TerminologyStore(tmp_path / "terminology.yaml")
    terminology.add_term(
        TermRule(source="ブラックホーク", target="「黑鹰」直升机", mode="hard")
    )
    config = _CORE._minimal_config(tmp_path)
    config.pipeline.factual_audit = False
    config.pipeline.chinese_reader_audit = False
    config.pipeline.max_repair_attempts = 1

    def failing(messages, _tier, _json_mode):
        payload = json.loads(messages[-1]["content"].split("\n", 1)[1])
        stable_id = payload["required_output"]["translations"][0]["id"]
        return json.dumps(
            {"translations": [{"id": stable_id, "target": "黑鹰"}]},
            ensure_ascii=False,
        )

    failed_client = FakeClient(failing)
    failed_pipeline = DirectPipeline(
        config,
        {role: failed_client for role in config.roles.model_dump()},
        config_dir=tmp_path,
    )
    with pytest.raises(RuntimeError, match="terminology_repair failed"):
        failed_pipeline.run(source)

    def recovering(messages, _tier, _json_mode):
        system = messages[0]["content"]
        payload = json.loads(messages[-1]["content"].split("\n", 1)[1])
        if system == REPAIR_SYSTEM:
            stable_id = payload["required_output"]["translations"][0]["id"]
            return json.dumps(
                {
                    "translations": [
                        {"id": stable_id, "target": "「黑鹰」直升机"}
                    ]
                },
                ensure_ascii=False,
            )
        if system == FIDELITY_SYSTEM:
            return json.dumps({"valid": True, "issues": []})
        raise AssertionError("completed translation must be resumed, not repeated")

    recovered_client = FakeClient(recovering)
    recovered_pipeline = DirectPipeline(
        config,
        {role: recovered_client for role in config.roles.model_dump()},
        config_dir=tmp_path,
    )
    store = recovered_pipeline.run(source)

    assert store.load_chapter(0).segments[0].target == "「黑鹰」直升机"
    events = Path(store.event_log_path).read_text(encoding="utf-8")
    assert "shadow_policy_invalidated" not in events
    assert "chapter_promoted" in events


def test_discovery_is_bounded_before_future_rule_but_remains_candidate(
    tmp_path: Path,
) -> None:
    store = TerminologyStore(tmp_path / "future-terminology.yaml")
    store.add_term(
        TermRule(
            source="コード",
            target="代号",
            mode="hard",
            status="active",
            valid_from=5,
        )
    )
    added = store.add_discoveries(
        2,
        [{"source": "コード", "target": "代码"}],
        "コード",
        "代码",
    )

    assert len(added) == 1
    assert added[0].status == "candidate"
    assert added[0].valid_from == 2
    assert added[0].valid_to == 4
