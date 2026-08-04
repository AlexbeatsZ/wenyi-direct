from __future__ import annotations

import importlib.util
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

_REWRITTEN = {"test_japanese_translation_guardrails_are_general_not_case_specific"}
for _name in dir(_CORE):
    if _name.startswith("test_") and _name not in _REWRITTEN:
        globals()[_name] = getattr(_CORE, _name)


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
