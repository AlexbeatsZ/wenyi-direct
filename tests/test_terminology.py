from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wenyi_direct.cli import app
from wenyi_direct.config import Config
from wenyi_direct.llm.providers.fake import FakeClient
from wenyi_direct.pipeline.direct import DirectPipeline
from wenyi_direct.pipeline.knowledge import (
    TerminologyDocument,
    TerminologyStore,
    TermRule,
    TranslationGroup,
)
from wenyi_direct.prompts import (
    CHINESE_READER_SYSTEM,
    FACTUAL_AUDIT_SYSTEM,
    FIDELITY_SYSTEM,
    REPAIR_SYSTEM,
    TRANSLATION_SYSTEM,
)


def test_groups_status_range_pronoun_and_preferred_visibility(tmp_path: Path) -> None:
    path = tmp_path / "terminology.yaml"
    store = TerminologyStore(
        path,
        TerminologyDocument(
            groups={"flame": TranslationGroup(source_anchor="炎", target_anchor="火焰")},
            terms=[
                TermRule(
                    source="炎魔法",
                    target="火焰魔法",
                    group_id="flame",
                    mode="hard",
                    status="active",
                ),
                TermRule(
                    source="黒騎士",
                    target="黑骑士",
                    mode="preferred",
                    status="active",
                    valid_from=1,
                    valid_to=3,
                    pronoun="neutral",
                ),
                TermRule(source="秘密", target="秘密", status="candidate"),
                TermRule(source="拒否", target="拒绝", status="rejected"),
            ],
        ),
    )
    store.save()
    loaded = TerminologyStore.load(path)
    visible = loaded.visible(2, "炎魔法を使う黒騎士。秘密と拒否。")
    assert visible["hard_terms"] == [
        {
            "source": "炎魔法",
            "target": "火焰魔法",
            "group": {"source_anchor": "炎", "target_anchor": "火焰"},
        }
    ]
    assert visible["preferred_terms"] == [
        {"source": "黒騎士", "target": "黑骑士", "pronoun": "neutral"}
    ]
    assert loaded.visible(4, "黒騎士")["preferred_terms"] == []
    assert loaded.visible(2, "彼女は走り出した。") == {
        "hard_terms": [],
        "preferred_terms": [],
    }


def test_group_members_must_contain_both_anchors() -> None:
    with pytest.raises(ValueError, match="target_anchor"):
        TerminologyDocument(
            groups={"flame": TranslationGroup(source_anchor="炎", target_anchor="火焰")},
            terms=[TermRule(source="炎術式", target="烈焰术式", group_id="flame")],
        )


def test_group_anchors_must_be_non_empty() -> None:
    with pytest.raises(ValueError, match="anchors must be non-empty"):
        TranslationGroup(source_anchor=" ", target_anchor="火焰")


def test_longest_term_wins_over_nested_short_term(tmp_path: Path) -> None:
    store = TerminologyStore(
        tmp_path / "terminology.yaml",
        TerminologyDocument(
            terms=[
                TermRule(source="炎", target="火焰"),
                TermRule(source="蒼炎", target="苍炎"),
            ]
        ),
    )
    assert store.hard_violations(0, "蒼炎が燃える", "苍炎燃烧") == []
    violations = store.hard_violations(0, "蒼炎が燃える", "苍火焰燃烧")
    assert len(violations) == 1
    assert violations[0]["required_target"] == "苍炎"


def test_discovery_auto_activates_soft_rule_but_conflict_becomes_candidate(
    tmp_path: Path,
) -> None:
    store = TerminologyStore(
        tmp_path / "terminology.yaml",
        TerminologyDocument(terms=[TermRule(source="ノエル", target="诺艾尔")]),
    )
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
        ("ブラックホーク", "active", "preferred"),
        ("ノエル", "candidate", "preferred"),
    ]
    loaded = TerminologyStore.load(store.path)
    assert loaded.visible(2, "ブラックホーク")["preferred_terms"][0]["target"] == "黑鹰"
    assert all(term["target"] != "诺埃尔" for term in loaded.visible(2, "ノエル")["hard_terms"])
    assert loaded.set_status("ノエル", "rejected", target="诺艾尔") == 1
    assert loaded.set_status("ノエル", "active", target="诺埃尔") == 1
    assert loaded.visible(2, "ノエル")["preferred_terms"][0]["target"] == "诺埃尔"


def test_discovery_accepts_a_stable_ordinary_noun_phrase(tmp_path: Path) -> None:
    store = TerminologyStore(tmp_path / "terminology.yaml")
    added = store.add_discoveries(
        3,
        [{"source": "焼き鳥屋", "target": "烤鸡串店"}],
        "今日も焼き鳥屋に寄った。",
        "今天也去了烤鸡串店。",
    )

    assert len(added) == 1
    assert added[0].model_dump(exclude_none=True) == {
        "source": "焼き鳥屋",
        "target": "烤鸡串店",
        "mode": "preferred",
        "status": "active",
        "valid_from": 3,
    }
    assert "普通名词短语" in FACTUAL_AUDIT_SYSTEM


def _minimal_config(tmp_path: Path) -> Config:
    return Config.model_validate(
        {
            "source_lang": "ja",
            "target_lang": "zh-CN",
            "state_dir": str(tmp_path / "state"),
            "terminology_file": str(tmp_path / "terminology.yaml"),
            "providers": {"default": {"provider": "fake"}},
            "roles": {
                role: "default"
                for role in (
                    "translate",
                    "factual_audit",
                    "chinese_audit",
                    "repair",
                    "validation",
                )
            },
            "window": {
                "max_read_chars": 1000,
                "max_write_chars": 1000,
                "source_halo_chars": 100,
            },
            "pipeline": {
                "chinese_reader_audit": False,
                "repair_context_segments": 0,
            },
        }
    )


def test_hard_term_is_repaired_even_when_model_audit_reports_no_issue(
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
            return json.dumps({"issues": [], "term_candidates": []})
        if system == REPAIR_SYSTEM:
            assert payload["knowledge"]["hard_terms"][0]["target"] == "亮了"
            stable_id = payload["required_output"]["translations"][0]["id"]
            return json.dumps(
                {"translations": [{"id": stable_id, "target": "亮了。"}]},
                ensure_ascii=False,
            )
        if system == FIDELITY_SYSTEM:
            assert payload["knowledge"]["hard_terms"][0]["target"] == "亮了"
            return json.dumps({"valid": True, "issues": []})
        if system == CHINESE_READER_SYSTEM:
            return json.dumps({"issues": []})
        raise AssertionError(system)

    config = _minimal_config(tmp_path)
    fake = FakeClient(handler)
    pipeline = DirectPipeline(
        config, {role: fake for role in config.roles.model_dump()}, config_dir=tmp_path
    )
    store = pipeline.run(source)
    assert store.load_chapter(0).segments[0].target == "亮了。"


def test_hard_term_violation_blocks_formal_promotion(tmp_path: Path) -> None:
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
        if system in {TRANSLATION_SYSTEM, REPAIR_SYSTEM}:
            stable_id = payload["required_output"]["translations"][0]["id"]
            return json.dumps(
                {"translations": [{"id": stable_id, "target": "闪光了。"}]},
                ensure_ascii=False,
            )
        if system == FACTUAL_AUDIT_SYSTEM:
            return json.dumps({"issues": [], "term_candidates": []})
        raise AssertionError(system)

    config = _minimal_config(tmp_path)
    config.pipeline.max_repair_attempts = 1
    fake = FakeClient(handler)
    pipeline = DirectPipeline(
        config, {role: fake for role in config.roles.model_dump()}, config_dir=tmp_path
    )
    with pytest.raises(RuntimeError, match="failed source-fidelity validation"):
        pipeline.run(source)
    formal = pipeline.store_for(source).load_chapter(0)
    assert formal.segments[0].target is None


def test_cli_manages_group_term_and_status(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """providers:\n  default:\n    provider: fake\npaths:\n  terminology_file: terminology.yaml\n""",
        encoding="utf-8",
    )
    runner = CliRunner()
    group = runner.invoke(
        app,
        [
            "terms",
            "group-add",
            "flame",
            "炎",
            "火焰",
            "--config",
            str(config),
        ],
    )
    assert group.exit_code == 0, group.output
    term = runner.invoke(
        app,
        [
            "terms",
            "add",
            "炎魔法",
            "火焰魔法",
            "--group",
            "flame",
            "--mode",
            "hard",
            "--status",
            "candidate",
            "--config",
            str(config),
        ],
    )
    assert term.exit_code == 0, term.output
    activated = runner.invoke(
        app,
        [
            "terms",
            "set-status",
            "炎魔法",
            "active",
            "--config",
            str(config),
        ],
    )
    assert activated.exit_code == 0, activated.output
    store = TerminologyStore.load(tmp_path / "terminology.yaml")
    assert store.terms[0].status == "active"
