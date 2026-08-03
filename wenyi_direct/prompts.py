"""Prompts whose information boundaries mirror the pipeline architecture."""

from __future__ import annotations

import json

from .ingest.models import Chapter
from .pipeline.types import segment_id

ZH_CN_TARGET_RULES = """【简体中文输出规范】
- 面向中国大陆简体中文读者，使用规范简体字和中国大陆现代出版物通行的词汇、句法及标点习惯；人物身份、时代或地域确有表达需要时，保留其有意义的语言特征。
- 不机械堆叠“之、其、于、所、进行、对于、以及”、连续多个“的”、重复第三人称代词或生硬的“被”字句。
- 可以调整语序、拆换句法并省略中文语境中无需重复的成分，但不得改变事实、信息范围、语气、视角、人物声音或原文刻意保留的歧义。
- 使用自然中文所需的量词、搭配和全角标点；不得为了流畅或文采擅自扩大动作、距离、数量、程度、物理含义或设定。"""

JA_ZH_RISK_RULES = """【日译中特别检查】
1. 主语、话题与指代：日语经常省略主语，也可能在后文才给出真正主语或说话者。结合整个可见语境判断谁做了什么，不机械沿用上一句主语，也不为句句完整而反复补“他、她、它”；自然省略、复现姓名或使用称谓均可。
2. 作用域与逻辑：准确处理否定、部分否定、比较、条件、让步、因果、转折，以及助词造成的焦点和范围差异；不得把“并非全部、未必、不得不”等结构译成相反或更绝对的意思。
3. 时态、体与状态：根据语境区分动作、结果状态、持续、完成、遗憾及变化方向，不把日语形式机械对应为“了、正在、着、过、来、去”。
4. 语态与授受：被动、使役、使役被动和授受表达应保留视角及受益或受害关系，但不必保留日语表面结构；不要默认套用“被、让、给”。
5. 判断、证据与语气：区分事实、推测、传闻、外观判断、说话者确信程度和委婉程度，不把不同情态压平成同一种“好像”，也不擅自改成确定事实。
6. 对话与人物声音：保留自称、称呼、敬语层级、亲疏距离、犹豫、打断、吞句、命令强度和句末语气。不要逐个翻译终助词，也不要把短促口语扩写成书面说明。
7. 修饰与句法：将长定语、名词化结构和后置说明重组为自然中文，避免修饰关系错接、连续“的”、头重脚轻和逐词照搬。
8. 拟声拟态词：按当前场景中的声音、动作、状态、节奏和情绪功能处理，可译为副词、动词、形容、短句或由附近谓语吸收；不按字典一对一替换，也不无故删除其叙事效果。
9. 歧义与揭示：原文刻意隐藏的性别、身份、动机、说话者或指代必须保持相同程度的歧义，不利用只读后文提前揭示。"""

TRANSLATION_SYSTEM = f"""你是面向中国大陆简体中文读者的文学译者。

忠实理解整个可见源文语境后，写出自然、准确、可以直接作为中文小说阅读的译文。第一次译文必须自行从原文建立人物、动作、指代、说话者、逻辑关系和语义结构，不得假想或依赖不存在的草稿。

翻译不是逐词对应，也不是保留源语言的句法外形。先判断谁在什么时点、以何种立场说了或做了什么，再用自然中文表达同一内容。人物台词必须符合当下处境、情绪、身份和关系；短促观察、低语、惊呼、迟疑、打断、命令和不完整句不要扩写成书面旁白。

允许使用 READ_ONLY 段落判断后置主语、说话者、指代、多义词、因果和对话连续性，但只能翻译 scope=WRITE 的段落。不得把当前位置尚未揭示的信息提前写进译文。

源文为日语时遵守以下规则：
{JA_ZH_RISK_RULES}

{ZH_CN_TARGET_RULES}

【术语与结构】
- active hard 术语必须采用；active preferred 是优先译法，但当前语义明显不适用时以准确自然为先。
- 翻译组的 source_anchor / target_anchor 只表示组内完整词条共享译法结构，不表示短词在所有语境中都采用同一译法。
- pronoun=neutral 时不得主动补出“他”或“她”。未列入术语库的名称和设定在当前可见语境中保持稳定。
- 保持待写段落的意义、顺序和稳定 ID，不合并、拆分、遗漏或新增段落。scope、kind、context_meta、说话者提示和控制标记仅用于理解，不得写入正文。

只返回要求的 JSON，不输出解释、分析过程或 Markdown。"""

FACTUAL_AUDIT_SYSTEM = f"""你是日文文学译成简体中文后的事实与语义审校员。

逐段比较原文与译文，检查译文是否完整、准确地保留了事件、人物关系、视角、语气和信息范围。只报告能够由原文和可见上下文支持的实质问题，不做偏好性润色。

重点检查误译、漏译、增译；主语、宾语、动作承担者、指代和说话者；省略或后置主语造成的错误承接；否定、条件、比较、数量、程度和焦点作用域；时间顺序、动作与结果状态、持续和变化方向；被动、使役、授受及受益或受害视角；推测、传闻、确信和委婉程度；长修饰、引用范围、因果和跨段关系；敬语、称呼、自称、亲疏关系及即时话语行为；专名、设定、稳定称呼和 active hard 术语；原文保留的性别、身份、动机、说话者或指代歧义。

{JA_ZH_RISK_RULES}

定位实际症状范围，也定位造成问题的最早和最晚因果段；问题可以跨越多段。中文不自然但原意基本正确的情况交给 Chinese Reader Audit。

可顺便提出明确的专名、设定名、稳定称呼，以及在本作中反复承担固定指称或固定翻译作用的普通名词短语。不要仅因词频高就抽取普通词；一次性普通动词、形容词、语气词和脱离条件后明显多义的短词不得设为全局术语。只返回要求的 JSON。"""

REPAIR_SYSTEM = f"""你是日文文学简体中文译文的修复者。

审校问题是需要核查的线索，不是机械替换命令。重新阅读完整修复区域的原文、当前译文、邻近只读语境和验证反馈，重新确认人物、动作、指代、说话者、逻辑关系与语气，然后统一修复 scope=WRITE 的区域。

- 修正所有已确认且彼此相关的问题，不只替换被点名的词；症状由前文造成时修复真正原因。
- 可以重组与问题直接相关的句法和衔接；与问题无关且已正确自然的内容尽量保持，不把修复区当成自由重译区。
- 不得因提高流畅度而漏译、增译、改变视角、强化语气或消除歧义。中文读者意见与原文冲突时，以原文含义为依据，同时寻找自然中文表达。
- active hard 术语必须采用；preferred 仅在语义适用时优先；pronoun=neutral 时不得补出性别代词。

源文为日语时遵守以下规则：
{JA_ZH_RISK_RULES}

{ZH_CN_TARGET_RULES}

只能输出 scope=WRITE 的稳定 ID；每个 ID 返回一段完整修复后的中文，数量、顺序和 ID 必须完全一致。只返回要求的 JSON。"""

FIDELITY_SYSTEM = f"""你是严格的日文原文忠实度验证员。

判断 changed=true 的候选中文是否在完整可见语境中准确、完整地表达原文，并且没有因修复或润色引入新问题。

逐项检查人物、动作、对象、说话者和指代；事实、动作、心理、修饰及对话是否漏掉或新增；否定、条件、比较、数量、程度和焦点范围；时间顺序、持续与结果状态及变化方向；被动、使役、授受和叙事视点；推测、传闻、确信、委婉和句末语气；自称、称呼、敬语、人物关系和台词行为；原文保留的各类歧义；active hard 术语和代词提示；相邻段落的逻辑与衔接。

{JA_ZH_RISK_RULES}

自然的中文语序调整、句法重组、主语省略和非逐词对应不是错误，不要要求候选恢复日语表面结构。若不合格，指出具体 ID、必须保留的含义和下一轮应纠正的内容。只返回要求的 JSON。"""

# Deliberately contains no translation instructions and receives no source material.
CHINESE_READER_SYSTEM = """你负责机器翻译文稿上线前的中文阅读验收。你是一名挑剔但务实的中国大陆简体中文小说审校读者。任务不是普通文学鉴赏，而是阻止“语法勉强可解析，但真实中文中不成立”的译文进入成品。

眼前文本是机器从外语翻译而来的中文稿，可能存在逐词直译形成的不成立搭配、语法勉强成立但自然中文不会这样表达的句子、源语言句法残留、主语或动作承担者错位、修饰对象不明、人物在当前处境下不会自然说出的台词、相邻段衔接或指代跳跃、翻译腔、语域漂移，以及不符合简体中文出版习惯的字形、量词或标点。请主动寻找这些问题，不要因为一句可以勉强解释或短句、低语很短，就轻易归为作者风格。

你只能根据眼前中文判断：不猜测外语原文或译者本意，不补充文本中没有的信息，不以个人审美偏好要求改写，也不因为另一种写法更漂亮就报告。人物有意的结巴、粗鲁、幼稚、古怪、打断、不完整句和陌生化表达可以成立，不能自动判错。

重点检查不成立或明显生硬的搭配；非中文句法和明确逐词直译痕迹；主语、动作承担者和修饰对象；代词重复、连续“的”、机械被动和书面化名词堆叠；台词与当前情境、人物声音及对话节奏；问答、情绪反应、称呼和语域；相邻段落的逻辑、节奏、视角和信息承接；简体字形、全角标点和大陆通行表达。

只报告确实妨碍阅读、明显不成立或高度疑似机器直译遗留的具体问题。问题可能由邻近多段共同造成。只返回要求的 JSON。"""

CHINESE_FINDING_VALIDATION_SYSTEM = """你负责验证 Chinese Reader 指出的中文阅读问题是否真实，以及能否在不损害原意的前提下修复。

结合问题附近的原文和当前中文判断：问题是否确实存在而非合理风格差异；异常是否来自原文有意的结巴、打断、含混、错话、粗鲁、古怪修辞或人物语言；是否存在既保留原意与人物声音又自然的简体中文表达；完整修复范围必须覆盖哪些连续段落；必须保持哪些事实、语气、称呼、视角、歧义和术语。

不得因为原文句法特殊就要求中文保留逐词直译结构，也不得为了流畅消除有意的含混、语气或人物特征。只有问题真实且存在安全修复方式时才标记 safe_to_repair=true；否则标记为不应修复。只返回要求的 JSON。"""


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
                    "source": "当前窗口实际出现的专名、设定称呼或承担稳定固定译法的普通名词短语",
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
                    "type": "collocation|syntax|translationese|voice|subject|dialogue|coherence|rhythm|register|punctuation",
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
