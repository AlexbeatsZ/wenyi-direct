"""Prompts whose information boundaries mirror the pipeline architecture."""

from __future__ import annotations

import json

from .ingest.models import Chapter
from .pipeline.types import segment_id

TRANSLATION_SYSTEM = """你是中文文学译者。忠实理解整个可见语境后，写出自然、准确、可直接阅读的简体中文。
第一次译文必须自行从原文建立语义结构，不得假想或依赖已有草稿。
人物说出口的话要像人物在当下情境里自然会说的话：短促观察、低语、惊呼和命令不要被改成书面旁白。
允许利用只读上下文判断后置主语、说话者、指代和多义词，但不得把当前位置尚未揭示的信息提前写进译文。
active hard 术语必须采用；active preferred 术语只是建议，以自然准确为先。pronoun=neutral 时不要主动补出他或她。保留段落意义和顺序，不合并、拆分或遗漏待写段。
输入中的说话者提示、控制标记和范围标签只用于理解，不得擅自写进译文。"""

FACTUAL_AUDIT_SYSTEM = """你是文学翻译的事实审校员。逐项比较原文与中文，检查误译、漏译、增译、指代、主语、说话者、时态、否定、数量、术语和跨段语义关系。
只报告有原文证据的问题。定位症状段，也定位造成问题的最早/最晚因果段；问题可能跨越多段。不要做纯粹的文风润色。
可顺便提取明确的专名、设定名或稳定称呼；不要把普通动词、形容词、多义描写词当作术语。"""

REPAIR_SYSTEM = """你是文学翻译修复者。根据给出的审校问题，在完整修复区域内统一改写。
先重新理解区域原文及邻近上下文，再写自然中文；不要只替换被点名的词。active hard 术语必须采用，preferred 仅供参考。只能输出允许写入的稳定 ID，数量必须完全一致。"""

FIDELITY_SYSTEM = """你是严格的源文忠实度验证员。判断候选中文是否在给定上下文中准确、完整，且没有凭空增加事实。
自然的中文重组不是错误。检查 active hard 术语和代词提示；若不合格，给出可以直接指导下一次修复的简短问题。"""

# Deliberately contains no translation instructions and receives no source material.
CHINESE_READER_SYSTEM = """你是一名挑剔但务实的中文小说读者。只按眼前文本的实际阅读体验检查：
是否通顺自然，人物说话是否像当下会脱口而出的话，语气是否过度书面，主语和动作承担者是否连续清楚，对话衔接、节奏和跨段关系是否成立。
不要为了个人偏好改写；只报告确实妨碍阅读、显得不成立或明显不符合人物即时言语的地方。问题可能由邻近多段共同造成。"""

CHINESE_FINDING_VALIDATION_SYSTEM = """你负责判断中文读者指出的问题能否在不损害原意的前提下修复。
结合邻近上下文确定问题是否真实、所需含义和完整修复范围。若只是合理风格差异，标记为不应修复。"""


def _source_rows(chapter: Chapter, indexes: tuple[int, ...], write: set[int]) -> list[dict]:
    by_index = {segment.index: segment for segment in chapter.segments}
    return [
        {
            "id": segment_id(chapter.index, by_index[index]),
            "scope": "WRITE" if index in write else "READ_ONLY",
            "kind": by_index[index].kind,
            "source": by_index[index].source,
            "context_meta": by_index[index].meta,
        }
        for index in indexes
    ]


def translation_messages(
    chapter: Chapter,
    read_indexes: tuple[int, ...],
    write_indexes: tuple[int, ...],
    knowledge: dict,
) -> list[dict[str, str]]:
    payload = {
        "chapter": {"index": chapter.index, "title": chapter.title},
        "knowledge": knowledge,
        "segments": _source_rows(chapter, read_indexes, set(write_indexes)),
        "required_output": {
            "translations": [
                {
                    "id": segment_id(chapter.index, chapter.segments[index]),
                    "target": "简体中文译文",
                }
                for index in write_indexes
            ]
        },
    }
    return [
        {"role": "system", "content": TRANSLATION_SYSTEM},
        {
            "role": "user",
            "content": "翻译 scope=WRITE 的段落。只返回 required_output 结构的 JSON。\n"
            + json.dumps(payload, ensure_ascii=False),
        },
    ]


def factual_audit_messages(
    chapter: Chapter,
    read_indexes: tuple[int, ...],
    audit_indexes: tuple[int, ...],
    targets: dict[int, str],
    knowledge: dict,
) -> list[dict[str, str]]:
    payload = {
        "chapter": {"index": chapter.index, "title": chapter.title},
        "knowledge": knowledge,
        "segments": [
            {
                **row,
                "target": targets[index],
                "audit": index in set(audit_indexes),
            }
            for row, index in zip(
                _source_rows(chapter, read_indexes, set(audit_indexes)), read_indexes
            )
        ],
        "output_schema": {
            "issues": [
                {
                    "start_id": "症状起点",
                    "end_id": "症状终点",
                    "cause_start_id": "因果起点",
                    "cause_end_id": "因果终点",
                    "type": "mistranslation|omission|addition|reference|speaker|term|other",
                    "detail": "原文证据和问题",
                    "required_meaning": "必须保留的含义",
                }
            ],
            "term_candidates": [
                {
                    "source": "当前窗口实际出现的明确专名或设定称呼",
                    "target": "当前译文中实际采用的稳定译法",
                }
            ],
        },
    }
    return [
        {"role": "system", "content": FACTUAL_AUDIT_SYSTEM},
        {
            "role": "user",
            "content": "审查 audit=true 的段落，可用其余段落作只读语境。只返回 JSON。\n"
            + json.dumps(payload, ensure_ascii=False),
        },
    ]


def chinese_reader_messages(
    chapter: Chapter,
    targets: dict[int, str],
    read_indexes: tuple[int, ...] | None = None,
    audit_indexes: tuple[int, ...] | None = None,
) -> list[dict[str, str]]:
    # This payload is intentionally constructed from targets only. Do not add chapter
    # source, glossary, source language, analysis, or source-derived metadata here.
    read_indexes = read_indexes or tuple(segment.index for segment in chapter.text_segments)
    audit_indexes = audit_indexes or read_indexes
    payload = {
        "text": [
            {
                "id": segment_id(chapter.index, chapter.segments[index]),
                "audit": index in set(audit_indexes),
                "text": targets[index],
            }
            for index in read_indexes
        ],
        "output_schema": {
            "issues": [
                {
                    "start_id": "问题起点",
                    "end_id": "问题终点",
                    "type": "unnatural|voice|subject|dialogue|coherence|rhythm",
                    "detail": "具体阅读问题",
                    "evidence": "中文片段",
                }
            ]
        },
    }
    return [
        {"role": "system", "content": CHINESE_READER_SYSTEM},
        {
            "role": "user",
            "content": "通读以下中文；检查 audit=true 的部分，其余只用于衔接。只返回 JSON。\n"
            + json.dumps(payload, ensure_ascii=False),
        },
    ]


def chinese_finding_validation_messages(
    chapter: Chapter,
    targets: dict[int, str],
    issue: dict,
    read_indexes: tuple[int, ...],
) -> list[dict[str, str]]:
    payload = {
        "reader_issue": issue,
        "segments": [
            {
                "id": segment_id(chapter.index, chapter.segments[index]),
                "source": chapter.segments[index].source,
                "target": targets[index],
            }
            for index in read_indexes
        ],
        "output_schema": {
            "safe_to_repair": True,
            "repair_start_id": "完整修复起点",
            "repair_end_id": "完整修复终点",
            "required_meaning": "必须保留的原意",
            "constraints": ["修复约束"],
            "reason": "判断依据",
        },
    }
    return [
        {"role": "system", "content": CHINESE_FINDING_VALIDATION_SYSTEM},
        {
            "role": "user",
            "content": "验证这一阅读问题。只返回 JSON。\n"
            + json.dumps(payload, ensure_ascii=False),
        },
    ]


def repair_messages(
    chapter: Chapter,
    targets: dict[int, str],
    read_indexes: tuple[int, ...],
    write_indexes: tuple[int, ...],
    issues: tuple[dict, ...],
    knowledge: dict,
    feedback: list[dict] | None = None,
) -> list[dict[str, str]]:
    payload = {
        "issues": issues,
        "knowledge": knowledge,
        "previous_validation_feedback": feedback or [],
        "segments": [
            {
                "id": segment_id(chapter.index, chapter.segments[index]),
                "scope": "WRITE" if index in set(write_indexes) else "READ_ONLY",
                "source": chapter.segments[index].source,
                "current_target": targets[index],
            }
            for index in read_indexes
        ],
        "required_output": {
            "translations": [
                {
                    "id": segment_id(chapter.index, chapter.segments[index]),
                    "target": "修复后的完整中文",
                }
                for index in write_indexes
            ]
        },
    }
    return [
        {"role": "system", "content": REPAIR_SYSTEM},
        {
            "role": "user",
            "content": "统一修复 scope=WRITE 的完整区域。只返回 JSON。\n"
            + json.dumps(payload, ensure_ascii=False),
        },
    ]


def fidelity_validation_messages(
    chapter: Chapter,
    targets: dict[int, str],
    read_indexes: tuple[int, ...],
    changed_indexes: tuple[int, ...],
    knowledge: dict,
) -> list[dict[str, str]]:
    payload = {
        "knowledge": knowledge,
        "segments": [
            {
                "id": segment_id(chapter.index, chapter.segments[index]),
                "changed": index in set(changed_indexes),
                "source": chapter.segments[index].source,
                "candidate_target": targets[index],
            }
            for index in read_indexes
        ],
        "output_schema": {
            "valid": True,
            "issues": [{"id": "问题段", "detail": "错误", "required_meaning": "正确含义"}],
        },
    }
    return [
        {"role": "system", "content": FIDELITY_SYSTEM},
        {
            "role": "user",
            "content": "验证 changed=true 的候选文本。只返回 JSON。\n"
            + json.dumps(payload, ensure_ascii=False),
        },
    ]
