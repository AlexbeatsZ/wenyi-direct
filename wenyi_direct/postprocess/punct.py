"""中文译文标点规范化。

确定性兜底（提示词已要求，这里再保一道）：
- ``quote_style=source`` 时，源文以 「」『』 为主就沿用直角引号；
- ``quote_style=zh-cn`` 时，日式引号 「」→ “”，『』→ ‘’；
- 英式直引号 "→ “/”（按出现次序配对），' → ‘/’（按次序配对，撇号尽量保留）；
- 半角 , . ! ? : ; 在中文语境（相邻为 CJK）→ 全角 ，。！？：；；
- 连续点号 ... / 。。。 / ・・・ → ……；-- 或 — → ——。

策略保守：英文/数字串内部的半角标点（如 9.11、Mr. Smith）不误伤——
仅当半角标点紧邻 CJK 字符时才转全角。
"""

from __future__ import annotations

import re
from typing import Literal

QuoteStyle = Literal["source", "zh-cn"]

_CJK = (
    "一-鿿"      # CJK 统一汉字
    "぀-ヿ"      # 假名（保险）
    "＀-￯"      # 全角符号
    "“”‘’（）《》【】、，。！？：；…—"
)
_CJK_RE = f"[{_CJK}]"

# 半角标点 → 全角
_HALF_TO_FULL = {",": "，", ".": "。", "!": "！", "?": "？", ":": "：", ";": "；"}


def _convert_quotes(
    text: str,
    *,
    double_open: bool = True,
    single_open: bool = True,
    preserve_corner: bool = False,
) -> tuple[str, bool, bool]:
    """转换日式和 ASCII 引号，并返回处理后的单双引号开闭状态。"""
    if not preserve_corner:
        text = text.translate(
            str.maketrans({"「": "“", "」": "”", "『": "‘", "』": "’"})
        )

    # 英式直双引号：按出现次序交替配对 → “ ”
    out = []
    for ch in text:
        if ch == '"':
            out.append("“" if double_open else "”")
            double_open = not double_open
        else:
            out.append(ch)
    text = "".join(out)

    # 直单引号：字母内撇号不改变引号状态；词尾撇号与右引号都输出 ’，
    # 但只有当前位于引语内时才关闭引号。
    out = []
    for index, ch in enumerate(text):
        if ch == "'":
            before = text[index - 1] if index else ""
            after = text[index + 1] if index + 1 < len(text) else ""
            before_letter = before.isascii() and before.isalpha()
            after_letter = after.isascii() and after.isalpha()
            if before_letter and after_letter:
                out.append("’")
            elif before_letter and not single_open:
                out.append("’")
                single_open = True
            elif before_letter:
                out.append("’")
            else:
                out.append("‘" if single_open else "’")
                single_open = not single_open
        else:
            out.append(ch)
    return "".join(out), double_open, single_open


def _convert_ellipsis_dash(text: str) -> str:
    """把多种省略号和破折号写法统一为中文双字符形式。"""
    text = re.sub(r"。{3,}", "……", text)
    text = re.sub(r"・{2,}", "……", text)
    text = re.sub(r"\.{3,}", "……", text)
    text = re.sub(r"…+", "……", text)          # 单个/多个 … → ……
    text = re.sub(r"-{2,}", "——", text)
    text = re.sub(r"—{1,}", "——", text)        # — / —— 归一为 ——
    return text


def _convert_halfwidth(text: str) -> str:
    """半角 ,.!?:; 紧邻 CJK 时转全角。"""
    def repl(m: re.Match) -> str:
        """按映射表替换一个已匹配的半角标点。"""
        return _HALF_TO_FULL[m.group(0)]

    # 标点左侧是 CJK 时转换；只与右侧 CJK 相邻时，若左侧是 ASCII
    # 字母/数字则保留，避免把 Mr.王、v2.版本 之类的边界误改。
    pattern = re.compile(
        rf"(?<={_CJK_RE})[,.!?:;]|[,.!?:;](?={_CJK_RE})"
    )
    return pattern.sub(
        lambda match: (
            match.group(0)
            if match.start() > 0 and text[match.start() - 1].isascii()
            and text[match.start() - 1].isalnum()
            else repl(match)
        ),
        text,
    )


def _normalize_with_quote_state(
    text: str,
    *,
    double_open: bool,
    single_open: bool,
    preserve_corner: bool = False,
) -> tuple[str, bool, bool]:
    """在给定引号状态下完成一段规范化，并返回新的状态。"""
    if not text:
        return text, double_open, single_open
    text, double_open, single_open = _convert_quotes(
        text,
        double_open=double_open,
        single_open=single_open,
        preserve_corner=preserve_corner,
    )
    text = _convert_ellipsis_dash(text)
    text = _convert_halfwidth(text)
    text = re.sub(r"([，。！？：；、])\s+", r"\1", text)
    text = re.sub(rf"([”’》】])\s+(?={_CJK_RE})", r"\1", text)
    return text, double_open, single_open


def _source_prefers_corner_quotes(sources: list[str]) -> bool:
    """源文出现直角引号时，视为作者选择了 「」『』 排版体系。"""
    return any(any(mark in source for mark in "「」『』") for source in sources)


def _curly_to_corner(text: str) -> str:
    """把中文弯引号改为直角引号；不把英文词内撇号误当成右引号。"""
    text = text.translate(str.maketrans({"“": "「", "”": "」"}))
    out: list[str] = []
    single_open = False
    for ch in text:
        if ch == "‘":
            out.append("『")
            single_open = True
        elif ch == "’" and single_open:
            out.append("』")
            single_open = False
        else:
            out.append(ch)
    return "".join(out)


def apply_source_quote_style(sources: list[str], targets: list[str]) -> list[str]:
    """若源文采用直角引号，把译文成对弯引号统一为同一排版风格。"""
    if len(sources) != len(targets):
        raise ValueError("sources 与 targets 数量必须一致")
    if not _source_prefers_corner_quotes(sources):
        return list(targets)
    return [_curly_to_corner(target) for target in targets]


def normalize_zh(
    text: str,
    *,
    quote_style: QuoteStyle = "zh-cn",
    source_text: str = "",
) -> str:
    """把一段中文译文的标点规范化为简体中文通用全角标点。"""
    normalized, _, _ = _normalize_with_quote_state(
        text,
        double_open=True,
        single_open=True,
        preserve_corner=quote_style == "source",
    )
    if quote_style == "source" and _source_prefers_corner_quotes([source_text]):
        normalized = _curly_to_corner(normalized)
    return normalized


def normalize_zh_segments(
    texts: list[str],
    continuations: list[bool] | None = None,
    *,
    quote_style: QuoteStyle = "zh-cn",
    sources: list[str] | None = None,
) -> list[str]:
    """按逻辑原段规范化标点，只在 cont=True 的切分续段间传递状态。

    普通段落即使缺失引号也不会改变下一段的开闭判断，避免错误级联污染后文。
    """
    if continuations is None:
        continuations = [False] * len(texts)
    if len(continuations) != len(texts):
        raise ValueError("texts 与 continuations 数量必须一致")
    if sources is not None and len(sources) != len(texts):
        raise ValueError("sources 与 texts 数量必须一致")

    normalized: list[str] = []
    double_open = True
    single_open = True
    for index, (text, continuation) in enumerate(zip(texts, continuations)):
        if index == 0 or not continuation:
            double_open = True
            single_open = True
        value, double_open, single_open = _normalize_with_quote_state(
            text,
            double_open=double_open,
            single_open=single_open,
            preserve_corner=quote_style == "source",
        )
        normalized.append(value)
    if quote_style == "source" and sources is not None:
        normalized = apply_source_quote_style(sources, normalized)
    return normalized


def restore_zh_dialogue_quotes(
    sources: list[str],
    targets: list[str],
    continuations: list[bool] | None = None,
    *,
    quote_style: QuoteStyle = "zh-cn",
) -> list[str]:
    """依据源文逻辑段边界补回模型遗漏的外层对话引号。

    只处理由 ``「」`` 或 ``『』`` 包住的完整逻辑段；``cont=True`` 的切分
    续段视为同一逻辑段。``source`` 沿用源文直角引号，``zh-cn`` 转为弯引号。
    """
    if continuations is None:
        continuations = [False] * len(targets)
    if len(sources) != len(targets) or len(continuations) != len(targets):
        raise ValueError("sources、targets 与 continuations 数量必须一致")

    restored = list(targets)
    group_start = 0
    for boundary in range(1, len(targets) + 1):
        if boundary < len(targets) and continuations[boundary]:
            continue
        group_sources = sources[group_start:boundary]
        if group_sources:
            source = "".join(group_sources).strip()
            first = restored[group_start]
            last = restored[boundary - 1]
            source_pair = next(
                (
                    pair
                    for pair in (("「", "」"), ("『", "』"))
                    if source.startswith(pair[0]) and source.endswith(pair[1])
                ),
                None,
            )
            if source_pair and first.strip() and last.strip():
                desired_pair = (
                    source_pair
                    if quote_style == "source"
                    else (("“", "”") if source_pair == ("「", "」") else ("‘", "’"))
                )
                leading = first[: len(first) - len(first.lstrip())]
                first_body = first.lstrip()
                if first_body.startswith(("“", "「", "‘", "『")):
                    first_body = first_body[1:]
                restored[group_start] = leading + desired_pair[0] + first_body
                last = restored[boundary - 1]
                trailing = last[len(last.rstrip()) :]
                last_body = last.rstrip()
                if last_body.endswith(("”", "」", "’", "』")):
                    last_body = last_body[:-1]
                restored[boundary - 1] = last_body + desired_pair[1] + trailing
        group_start = boundary
    return restored
