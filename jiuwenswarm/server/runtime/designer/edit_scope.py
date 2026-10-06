# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep a color edit on the named object when a similar object is qualified differently."""

from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from typing import Any

logger = logging.getLogger(__name__)

_CN_COLORS = (
    "白色", "红色", "黑色", "蓝色", "绿色", "黄色", "灰色", "棕色", "紫色",
    "橙色", "粉色", "金色", "银色", "青色", "褐色",
    "白", "红", "黑", "蓝", "绿", "黄", "灰", "棕", "紫", "橙", "粉", "金", "银", "青", "褐",
)
_EN_COLORS = (
    "white", "red", "black", "blue", "green", "yellow", "gray", "grey",
    "brown", "purple", "orange", "pink", "gold", "silver",
)
_CN_CHANGE = re.compile(
    r"(?:把|将)(?P<old>.{1,80}?)(?:统一)?(?:改成|改为|换成|变成)(?P<new>.{1,40}?)(?:[，。,.;；]|$)"
)
_EN_CHANGE = re.compile(
    r"\b(?:change|recolor|turn)\s+(?P<old>.{1,80}?)\s+(?:in)?to\s+(?P<new>.{1,40}?)(?:[.,;]|$)",
    re.I,
)
_ALL_SCOPE = re.compile(r"所有|全部|每一|全都|凡是|\ball\b|\bevery\b|\beach\b", re.I)
_MEASURE_PREFIX = re.compile(
    r"^(?:[0-9一二两三四五六七八九十几数]+)?(?:只|个|条|辆|张|件|台|些|双|对)"
)
_EN_STOP = frozenset({
    "the", "this", "that", "these", "those", "one", "its", "his", "her", "their", "same",
})


class _Substitution:
    def __init__(self, old_color: str, new_color: str, head: str, old_span: str, english: bool) -> None:
        self.old_color = old_color
        self.new_color = new_color
        self.head = head
        self.old_span = old_span
        self.english = english


def restore_graph_attribute_scope(
    before: dict[str, Any],
    after: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    """Put a spilled color back on objects the user did not name.

    The requested object still takes the new color. A same-class object with an
    extra qualifier, such as a background prop beside the named prop, keeps the
    color it had before the edit. This runs before document sync and again
    before save, so neither the graph nor a later prose pass can persist the spill.
    """
    sub = _substitution(message)
    if sub is None:
        return after
    before_corpus = _graph_corpus(before)
    qualifiers = _qualifiers(before_corpus, sub)
    if not qualifiers:
        return after
    restored = deepcopy(after)
    if isinstance(restored.get("description"), str):
        restored["description"] = _restore(before_corpus, restored["description"], qualifiers, sub)
    for node in restored.get("nodes") or []:
        config = node.get("config")
        if isinstance(config, dict):
            node["config"] = _map_strings(config, lambda text: _restore(before_corpus, text, qualifiers, sub))
    if _graph_corpus(restored) == _graph_corpus(after):
        return after
    logger.info("Restored a color edit that spilled onto a differently qualified object")
    return restored


def restore_document_attribute_scope(
    before_graph: dict[str, Any],
    before_documents: dict[str, str],
    texts: dict[str, str],
    message: str,
) -> dict[str, str]:
    """Restore the same spill in brief and storyboard text after the prose pass."""
    sub = _substitution(message)
    if sub is None:
        return texts
    corpus = "\n".join([_graph_corpus(before_graph), *before_documents.values()])
    qualifiers = _qualifiers(corpus, sub)
    if not qualifiers:
        return texts
    restored = {
        key: _restore(corpus, value, qualifiers, sub)
        for key, value in texts.items()
    }
    if restored != texts:
        logger.info("Restored a color edit that spilled onto a differently qualified object")
    return restored


def _substitution(message: str) -> _Substitution | None:
    text = str(message or "").strip()
    if not text:
        return None
    match = _CN_CHANGE.search(text)
    if match and not _ALL_SCOPE.search(match.group("old")):
        found = _cn_substitution(match.group("old"), match.group("new"))
        if found is not None:
            return found
    match = _EN_CHANGE.search(text)
    if match and not _ALL_SCOPE.search(match.group("old")):
        return _en_substitution(match.group("old"), match.group("new"))
    return None


def _cn_substitution(old_span: str, new_span: str) -> _Substitution | None:
    old_colors = _cn_colors(old_span)
    new_colors = _cn_colors(new_span)
    old_color = next((item for item in old_colors if item not in new_colors), "")
    new_color = next((item for item in new_colors if item not in old_colors), "")
    if not old_color or not new_color or old_color == new_color:
        return None
    old_tail = _cn_tail(old_span, old_color)
    new_tail = _cn_tail(new_span, new_color)
    head = _common_suffix(old_tail, new_tail) or (old_tail[-1] if old_tail else "")
    if not head or head in "的了和与在把将中":
        return None
    return _Substitution(old_color, new_color, head, old_span, False)


def _en_substitution(old_span: str, new_span: str) -> _Substitution | None:
    old_color = _en_color(old_span)
    new_color = _en_color(new_span)
    if not old_color or not new_color or old_color == new_color:
        return None
    old_tail = _en_tail(old_span, old_color)
    new_tail = _en_tail(new_span, new_color)
    head = old_tail or new_tail
    if not head:
        return None
    return _Substitution(old_color, new_color, head, old_span, True)


def _qualifiers(corpus: str, sub: _Substitution) -> list[str]:
    found: list[str] = []
    for match in _source_pattern(sub).finditer(corpus):
        raw = match.group("prefix")
        qualifier = raw.strip() if sub.english else _clean_qualifier(raw, sub.old_span)
        if sub.english and (
            qualifier.lower() in _EN_STOP or qualifier.lower() in sub.old_span.lower()
        ):
            qualifier = ""
        if qualifier and qualifier not in found:
            found.append(qualifier)
    return found


def _restore(before_corpus: str, text: str, qualifiers: list[str], sub: _Substitution) -> str:
    for qualifier in qualifiers:
        pattern = _leak_pattern(qualifier, sub)
        if pattern.search(before_corpus):
            continue
        text = pattern.sub(lambda match: _swap_color(match.group(0), sub), text)
    return text


def _source_pattern(sub: _Substitution) -> re.Pattern[str]:
    if sub.english:
        return re.compile(
            rf"(?P<prefix>\b[A-Za-z]{{3,}}\b)\s+{_en_color_expr(sub.old_color)}\s+{_en_head(sub.head)}\b",
            re.I,
        )
    return re.compile(
        rf"(?P<prefix>[\u4e00-\u9fff与和及跟]{{0,8}})(?:的)?{_cn_color_expr(sub.old_color)}(?:[\u4e00-\u9fff]{{0,8}})?{re.escape(sub.head)}"
    )


def _leak_pattern(qualifier: str, sub: _Substitution) -> re.Pattern[str]:
    if sub.english:
        return re.compile(
            rf"\b{re.escape(qualifier)}\s+{_en_color_expr(sub.new_color)}\s+{_en_head(sub.head)}\b",
            re.I,
        )
    return re.compile(
        rf"{re.escape(qualifier)}(?:的)?{_cn_color_expr(sub.new_color)}(?:[\u4e00-\u9fff]{{0,8}})?{re.escape(sub.head)}"
    )


def _clean_qualifier(prefix: str, old_span: str) -> str:
    token = str(prefix or "")
    token = re.sub(r"^.*[与和及跟、]", "", token)
    token = _MEASURE_PREFIX.sub("", token)
    token = re.sub(r"(?:只|个|条|辆|张|件|台|些|双|对)+$", "", token)
    if re.search(r"[只个条辆张件台些0-9]", token):
        return ""
    if not 2 <= len(token) <= 4 or token in old_span:
        return ""
    return token


def _swap_color(piece: str, sub: _Substitution) -> str:
    if sub.english:
        return re.sub(re.escape(sub.new_color), sub.old_color, piece, count=1, flags=re.I)
    for new_form, old_form in (
        (sub.new_color + "色", sub.old_color + "色"),
        (sub.new_color, sub.old_color),
    ):
        if new_form in piece:
            return piece.replace(new_form, old_form, 1)
    return piece


def _cn_colors(text: str) -> list[str]:
    found: list[str] = []
    index = 0
    words = sorted(_CN_COLORS, key=len, reverse=True)
    while index < len(text):
        for word in words:
            if text.startswith(word, index):
                canon = word[:-1] if word.endswith("色") else word
                if canon not in found:
                    found.append(canon)
                index += len(word)
                break
        else:
            index += 1
    return found


def _cn_tail(text: str, color: str) -> str:
    forms = (color + "色", color)
    start = -1
    matched = ""
    for form in forms:
        at = text.find(form)
        if at >= 0 and (start < 0 or at < start):
            start = at
            matched = form
    if start < 0:
        return ""
    tail = text[start + len(matched):]
    chunk = re.match(r"[\u4e00-\u9fff]+", tail)
    return chunk.group(0) if chunk else ""


def _cn_color_expr(color: str) -> str:
    return "(?:" + re.escape(color + "色") + "|" + re.escape(color) + ")"


def _en_color(text: str) -> str:
    match = re.search(r"\b(" + "|".join(_EN_COLORS) + r")\b", text, re.I)
    return match.group(1).lower() if match else ""


def _en_tail(text: str, color: str) -> str:
    match = re.search(rf"\b{re.escape(color)}\b(?:\s+\w+){{0,3}}?\s+([A-Za-z]+)", text, re.I)
    if match is None:
        return ""
    word = match.group(1).lower()
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def _en_color_expr(color: str) -> str:
    return re.escape(color)


def _en_head(head: str) -> str:
    return re.escape(head) + "s?"


def _common_suffix(left: str, right: str) -> str:
    if not left or not right:
        return ""
    size = 0
    while size < len(left) and size < len(right) and left[-1 - size] == right[-1 - size]:
        size += 1
    return left[-size:] if size else ""


def _graph_corpus(graph: dict[str, Any]) -> str:
    parts = [str(graph.get("description") or "")]
    for node in graph.get("nodes") or []:
        if isinstance(node, dict):
            parts.append(json.dumps(node.get("config") or {}, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts)


def _map_strings(value: Any, fn) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {key: _map_strings(item, fn) for key, item in value.items()}
    if isinstance(value, list):
        return [_map_strings(item, fn) for item in value]
    return value
