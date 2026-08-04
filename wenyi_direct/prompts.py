"""Prompts whose information boundaries mirror the pipeline architecture."""

from __future__ import annotations

import json

from .ingest.models import Chapter
from .pipeline.types import segment_id

TRANSLATION_SYSTEM = """你是中文文学译者。忠实理解整个可见语境后，写出自然、准确、可直接阅读的简体中文。
第一次译文必须自行从原文建立语义结构，不得假想或依赖已有草稿。
人物说出口的话要像人物在当下情境里自然会说的话：短促观察、低语、惊呼和命令不要被改成书面旁白。
允许利用只读上下文判断后置主语、说话者、指代和多义词，但不得把当前位置尚未揭示的信息提前写进译文。
源文为日语时，先还原省略的主语、动作承担者、句间关系和对话功能，再按中文重组；不照搬日语的名词句、连体修饰顺序、后置补充或词性。普通词根据当前语境和话语功能选择中文说法，不把词典义机械拼成台词或描写。可以保留短句和破碎节奏，但不能因此留下主谓错位、不成立搭配或失去问句、命令、引语层级等交际功能。按中文习惯处理量词和标点，同时不得为了自然或文采擅自扩大动作、距离、程度、物理含义或设定。
active hard 术语必须采用；active preferred 术语只是建议，以自然准确为先。pronoun=neutral 时不要主动补出他或她。保留段落意义和顺序，不合并、拆分或遗漏待写段。
输入中的说话者提示、控制标记和范围标签只用于理解，不得擅自写进译文。
输出 translations 的条目数、ID 集合和顺序必须与 required_output 完全一致；READ_ONLY 段不得输出。"""

FACTUAL_AUDIT_SYSTEM = """你是文学翻译的事实审校员。逐项比较原文与中文，检查误译、漏译、增译、指代、主语、说话者、时态、否定、数量、术语和跨段语义关系。
只报告有原文证据的问题。定位症状段，也定位造成问题的最早/最晚因果段；问题可能跨越多段。不要做纯粹的文风润色。
输入中的 current_terminology 只是当前翻译采用的既有约定，不是不可质疑的事实。即使某条约定当前标记为 hard，也要根据原文和作品语境检查译名本身、适用范围和多义性是否正确；不得为了服从旧术语而放过明确误译。
若证据足以说明某一条完整的现行术语规则本身错误，而不只是当前语境例外，可提出 entire_existing_rule 修订，并列出直接支持判断的 evidence_ids。若只是多义词在当前句不适用、范围需要拆分或证据仍不足，应报告普通 term 问题，不得提出全规则替换。
可顺便提取明确的专名、设定名、稳定称呼，以及在本作中反复承担固定指称或固定翻译作用的普通名词短语。每个候选必须指出本窗口中支持该对应关系的 segment_id。不要仅因词频高就抽取普通词；多义动词、形容词和描写词只有在当前语义条件明确、确实形成稳定设定表达时才可提出。
短句也必须检查词典义直拼造成的不成立中文搭配，并依据上下文中的话语功能判断是否误译；不要因为字数少就略过。"""

REPAIR_SYSTEM = """你是文学翻译修复者。根据给出的审校问题，在完整修复区域内统一改写。
先重新理解区域原文及邻近上下文，再写自然中文；不要只替换被点名的词。active hard 术语必须采用，preferred 仅供参考；若审校反馈把 preferred 误当成强制要求，必须以当前源文语义为准。只能输出允许写入的稳定 ID，数量必须完全一致。"""

FIDELITY_SYSTEM = """你是严格的源文忠实度验证员。逐一检查每个 changed=true 的稳定 ID，判断候选中文是否在给定上下文中准确、完整，且没有凭空增加事实；任何一个 changed ID 不合格，整体 valid 必须为 false。
自然的中文重组不是错误。current_terminology 只是现行译法约定，不是不可质疑的真理；若当前源文明确表明现行译名错误、范围过宽或语义不适用，应据此报告，而不是要求候选盲目改回。preferred 更不能作为质量门。若不合格，给出可以直接指导下一次修复的简短问题。"""

TERM_REVISION_VALIDATION_SYSTEM = """你负责独立验证一项术语全规则修订建议。
只有证据足以证明当前 source→old_target 这一整条既有规则本身错误，并且 new_target 可安全替代该规则适用范围内的旧译名时，才批准 entire_existing_rule 修订。
若问题只发生在当前语境、该源词存在需要分流的多义用法、证据不足以覆盖整条规则，或新译名只是风格偏好，必须拒绝全规则修订。现行 hard 标记不是正确性的证明，也不得阻止你质疑旧译名。只返回要求的 JSON。"""

TERM_MIGRATION_REPAIR_SYSTEM = """你负责修复一处无法机械迁移的既有译文。
术语规则已经确认应从 old_target 改为 new_target。结合当前源文和邻近上下文，只重写 scope=WRITE 的完整中文，使对应 source 明确采用 new_target，同时保持其余事实、语气、人物声音和自然中文不变。不要把普通描写中碰巧相同的词一并替换。只返回 required_output 结构的 JSON。"""

# Deliberately contains no translation instructions and receives no source material.
CHINESE_READER_SYSTEM = """你负责机器翻译文稿上线前的中文阅读验收。你的任务不是普通文学鉴赏，而是阻止“语法勉强可解析，但真实中文中不成立”的译文进入成品。
眼前文本是机器从外语翻译而来的中文稿，可能存在逐词直译形成的不成立搭配、中文读者不会这样说的句子、不像人物当下会脱口而出的台词、主语或动作承担者错位、上下段衔接断裂、翻译腔、异常量词或引号。请主动寻找这些问题，尤其不要因为短句或低语很短就忽略其搭配和语气。
你看不到原文，所以只判断中文自身是否成立；不要猜测原意，不要把个人措辞偏好当成错误，也不要因为它是译文就要求全面润色。短促、破碎或陌生化表达可以是文学风格，但人物在具体情境下不可能自然说出的搭配仍应报告。
通读上下文后，只报告确实影响理解、自然度、人物声音或连贯性的具体问题。问题可能由邻近多段共同造成。"""

CHINESE_FINDING_VALIDATION_SYSTEM = """你负责判断中文读者指出的问题能否在不损害原意的前提下修复。
结合邻近上下文确定问题是否真实、所需含义和完整修复范围。current_terminology 只是可质疑的现行约定；若原文证据与其冲突，不得为了维持旧译名而否决真实问题。若只是合理风格差异，标记为不应修复。"""


def _source_rows(chapter: Chapter, indexes: tuple[int, ...], write: set[int]) -> list[dict]:
    by_index = {segment.index: segment for segment in chapter.segments}
    return [
        {
            "id": segment_id(chapter.index, by_index[index]),
            "scope": "WRITE" if index in write else "READ_ONLY",
            "kind": by_index[index].kind,
            "source": by_index[index].source,
            "continuation": by_index[index].cont,
        }
        for index in indexes
    ]


def _challengeable_knowledge(knowledge: dict | None) -> dict:
    """Downgrade generation constraints to challengeable conventions for reviewers."""
    original = dict(knowledge or {})
    conventions = list(original.pop("current_terminology", []) or [])
    for mode, key in (("hard", "hard_terms"), ("preferred", "preferred_terms")):
        for item in original.pop(key, []) or []:
            if not isinstance(item, dict):
                continue
            convention = {
                "source": item.get("source"),
                "current_target": item.get("target"),
                "current_mode": mode,
                "current_status": "active",
                "challengeable": True,
            }
            for optional in ("group", "pronoun", "valid_from", "valid_to"):
                if optional in item:
                    convention[optional] = item[optional]
            conventions.append(convention)
    original["current_terminology"] = conventions
    return original


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
        "knowledge": _challengeable_knowledge(knowledge),
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
                    "segment_id": "支持该对应关系的当前窗口稳定 ID",
                    "source": "当前窗口实际出现的专名、设定称呼或承担稳定固定译法的普通名词短语",
                    "target": "当前译文中实际采用的稳定译法",
                }
            ],
            "term_revisions": [
                {
                    "source": "现行完整术语源词",
                    "old_target": "当前规则译名",
                    "new_target": "建议替换后的译名",
                    "scope": "entire_existing_rule",
                    "evidence_ids": ["直接支持全规则修订的当前窗口稳定 ID"],
                    "reason": "为什么旧规则整体错误，而非单句例外",
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


def term_revision_validation_messages(
    chapter: Chapter,
    revision: dict,
    read_indexes: tuple[int, ...],
    targets: dict[int, str],
    knowledge: dict,
) -> list[dict[str, str]]:
    payload = {
        "revision": revision,
        "knowledge": _challengeable_knowledge(knowledge),
        "segments": [
            {
                "id": segment_id(chapter.index, chapter.segments[index]),
                "source": chapter.segments[index].source,
                "target": targets[index],
            }
            for index in read_indexes
        ],
        "output_schema": {
            "approved": True,
            "reason": "批准或拒绝全规则修订的依据",
        },
    }
    return [
        {"role": "system", "content": TERM_REVISION_VALIDATION_SYSTEM},
        {
            "role": "user",
            "content": "验证该 entire_existing_rule 术语修订。只返回 JSON。\n"
            + json.dumps(payload, ensure_ascii=False),
        },
    ]


def term_migration_repair_messages(
    chapter: Chapter,
    revision: dict,
    read_indexes: tuple[int, ...],
    write_index: int,
    targets: dict[int, str],
) -> list[dict[str, str]]:
    payload = {
        "revision": revision,
        "segments": [
            {
                "id": segment_id(chapter.index, chapter.segments[index]),
                "scope": "WRITE" if index == write_index else "READ_ONLY",
                "source": chapter.segments[index].source,
                "current_target": targets[index],
            }
            for index in read_indexes
        ],
        "required_output": {
            "translations": [
                {
                    "id": segment_id(chapter.index, chapter.segments[write_index]),
                    "target": "迁移术语后的完整中文",
                }
            ]
        },
    }
    return [
        {"role": "system", "content": TERM_MIGRATION_REPAIR_SYSTEM},
        {
            "role": "user",
            "content": "修复 scope=WRITE 的术语迁移歧义。只返回 JSON。\n"
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
    issues: list[dict],
    read_indexes: tuple[int, ...],
    knowledge: dict | None = None,
) -> list[dict[str, str]]:
    payload = {
        "reader_issues": issues,
        "knowledge": _challengeable_knowledge(knowledge),
        "segments": [
            {
                "id": segment_id(chapter.index, chapter.segments[index]),
                "source": chapter.segments[index].source,
                "target": targets[index],
            }
            for index in read_indexes
        ],
        "output_schema": {
            "results": [
                {
                    "finding_id": "必须原样返回 reader_issues.finding_id",
                    "safe_to_repair": True,
                    "repair_start_id": "完整修复起点；不可修复时可省略",
                    "repair_end_id": "完整修复终点；不可修复时可省略",
                    "required_meaning": "必须保留的原意",
                    "constraints": ["修复约束"],
                    "reason": "判断依据",
                }
            ]
        },
    }
    return [
        {"role": "system", "content": CHINESE_FINDING_VALIDATION_SYSTEM},
        {
            "role": "user",
            "content": "逐条验证这些阅读问题，每个 finding_id 恰好返回一个结果。只返回 JSON。\n"
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
    def issue_view(issue: dict) -> dict:
        allowed = (
            "finding_id",
            "type",
            "detail",
            "evidence",
            "required_meaning",
            "constraints",
            "reason",
            "start_id",
            "end_id",
            "cause_start_id",
            "cause_end_id",
        )
        view = {key: issue[key] for key in allowed if key in issue}
        violation = issue.get("terminology_violation")
        if isinstance(violation, dict):
            view["hard_terminology"] = {
                key: violation[key]
                for key in ("source", "required_target")
                if key in violation
            }
        return view

    payload = {
        "issues": [issue_view(issue) for issue in issues],
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
        "knowledge": _challengeable_knowledge(knowledge),
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
