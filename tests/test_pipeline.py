from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from wenyi_direct.prompts import (
    FACTUAL_AUDIT_SYSTEM,
    FIDELITY_SYSTEM,
    TRANSLATION_SYSTEM,
)

_CORE_PATH = Path(__file__).with_name("pipeline_core_cases.py")
_SPEC = importlib.util.spec_from_file_location("wenyi_pipeline_core_cases", _CORE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_CORE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _CORE
_SPEC.loader.exec_module(_CORE)

# Legacy pipeline tests predate the optional post-language-repair Chinese recheck.
# Keep their original scope stable; dedicated tests/test_language_recheck.py covers
# the new default behaviour.
_ORIGINAL_CONFIG = _CORE._config


def _legacy_config(tmp_path):
    config = _ORIGINAL_CONFIG(tmp_path)
    config.pipeline.max_language_rechecks = 0
    return config


_CORE._config = _legacy_config

_REWRITTEN = {
    "test_full_pipeline_and_chinese_audit_information_boundary",
    "test_japanese_translation_guardrails_are_general_not_case_specific",
}
for _name in dir(_CORE):
    if _name.startswith("test_") and _name not in _REWRITTEN:
        globals()[_name] = getattr(_CORE, _name)


def test_full_pipeline_and_chinese_audit_information_boundary(tmp_path: Path) -> None:
    source = tmp_path / "book.json"
    _CORE._write_book(source)
    config = _CORE._config(tmp_path)
    fake = _CORE.FakeClient(_CORE._handler)
    clients = {role: fake for role in config.roles.model_dump()}
    pipeline = _CORE.DirectPipeline(config, clients, config_dir=tmp_path)

    store = pipeline.run(source, chapters={0})
    chapter = store.load_chapter(0)
    assert [segment.target for segment in chapter.text_segments] == [
        "他来了。",
        "亮了。",
        "诺艾尔的低语被轰鸣声淹没了。",
    ]
    manifest = store.load_manifest()
    assert manifest["chapters"][0]["status"] == "done"
    assert manifest["chapters"][1]["status"] == "pending"
    assert manifest["future_chapters_required"] is False
    discovered = next(term for term in pipeline.terminology.terms if term.source == "ノエル")
    assert discovered.mode == "preferred"
    assert discovered.status == "active"
    assert discovered.valid_from == 0
    assert not (tmp_path / "terminology.yaml").exists()
    assert (Path(store.run_dir) / "terminology.yaml").exists()

    chinese_calls = [
        call
        for call in fake.calls
        if call["messages"][0]["content"] == _CORE.CHINESE_READER_SYSTEM
    ]
    assert len(chinese_calls) == 1
    serialized = json.dumps(chinese_calls[0]["messages"], ensure_ascii=False)
    for source_text in ("彼が来た。", "光った。", "ノエル", "未来の秘密。", "夜の章"):
        assert source_text not in serialized
    all_calls = json.dumps(fake.calls, ensure_ascii=False)
    assert "未来の秘密。" not in all_calls

    language_repair_calls = [
        call
        for call in fake.calls
        if call["messages"][0]["content"] == _CORE.REPAIR_SYSTEM
        and _CORE._payload(call["messages"])["issues"][0]["type"] == "unnatural"
    ]
    assert len(language_repair_calls) == 2
    assert sorted(
        len(_CORE._payload(call["messages"])["required_output"]["translations"])
        for call in language_repair_calls
    ) == [1, 1]

    calls_before_second_chapter = len(fake.calls)
    pipeline.run(source, chapters={1})
    second_chapter_calls = json.dumps(
        fake.calls[calls_before_second_chapter:], ensure_ascii=False
    )
    assert "未来の秘密。" in second_chapter_calls
    assert "彼が来た。" in second_chapter_calls
    assert "他来了。" in second_chapter_calls

    artifact = Path(store.translation_artifact_path(0)).read_text(encoding="utf-8")
    assert "direct_translation" in artifact
    assert "factual_repair_proposal" in artifact
    assert "language_repair_accepted" in artifact
    assert "formal_promotion" in artifact

    epub_path = tmp_path / "book.zh.epub"
    _CORE.assemble(store, str(source), str(epub_path), out_format="epub")
    assert _CORE.validate_epub(epub_path)["ok"] is True


def test_japanese_translation_guardrails_are_general_not_case_specific() -> None:
    for principle in (
        "省略的主语",
        "话语功能",
        "连体修饰顺序",
        "不成立搭配",
        "不得为了自然或文采擅自扩大",
    ):
        assert principle in TRANSLATION_SYSTEM
    for case_specific_answer in ("光った", "闪光了", "亮了", "第一具猎物"):
        assert case_specific_answer not in TRANSLATION_SYSTEM
    assert "current_terminology" in FIDELITY_SYSTEM
    assert "preferred 更不能作为质量门" in FIDELITY_SYSTEM
    assert "逐一检查每个 changed=true" in FIDELITY_SYSTEM
    assert "条目数、ID 集合和顺序" in TRANSLATION_SYSTEM
    assert "词典义直拼" in FACTUAL_AUDIT_SYSTEM
    assert "entire_existing_rule" in FACTUAL_AUDIT_SYSTEM
