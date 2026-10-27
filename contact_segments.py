"""主动联系消息跟随本体分段配置，保持文本与媒体顺序。"""

import math
import random
import re

from astrbot.api import logger
from astrbot.api.message_components import At, Plain, Reply


def split_text(text, settings):
    if "```" in text or len(text) > int(settings.get("words_count_threshold", 150)):
        return [text]
    if settings.get("split_mode", "regex") == "words":
        words = settings.get("split_words", ["。", "？", "！", "~", "…"])
        if not words:
            return [text]
        pattern = "|".join(
            re.escape(word) for word in sorted(words, key=len, reverse=True) if word
        )
        if not pattern:
            return [text]
        ends = [match.end() for match in re.finditer(pattern, text)]
        starts = [0, *ends]
        pieces = [text[start:end] for start, end in zip(starts, [*ends, len(text)])]
    else:
        try:
            matches = list(
                re.finditer(
                    settings.get("regex", r".*?[。？！~…\n]+|.+$"), text, re.S | re.M
                )
            )
        except re.error:
            logger.warning("主动联系分段正则无效，保留原文发送。")
            return [text]
        pieces, cursor = [], 0
        for match in matches:
            if match.end() == match.start():
                continue
            if match.start() > cursor:
                pieces.append(text[cursor : match.start()])
            pieces.append(match.group(0))
            cursor = match.end()
        pieces.append(text[cursor:])
    cleanup = settings.get("content_cleanup_rule", "")
    if cleanup:
        try:
            compiled = re.compile(cleanup)
        except re.error:
            logger.warning("主动联系内容清理正则无效，保留分段文本。")
        else:
            pieces = [compiled.sub("", part) for part in pieces]
    return [part.strip() for part in pieces if part.strip()]


def build_parts(chain, settings, enabled=True, model_result=True):
    segmented = (
        enabled
        and settings.get("enable", False)
        and (model_result or not settings.get("only_llm_result", True))
    )
    if not segmented:
        return [list(chain)] if chain else []
    parts, prefix = [], []
    for component in chain:
        if isinstance(component, (At, Reply)):
            prefix.append(component)
            continue
        components = (
            [Plain(text=text) for text in split_text(component.text, settings)]
            if isinstance(component, Plain)
            else [component]
        )
        for item in components:
            parts.append([*prefix, item])
            prefix = []
    if prefix:
        if parts:
            parts[-1].extend(prefix)
        else:
            parts.append(prefix)
    return parts


def segment_delay(part, settings):
    text = "".join(comp.text for comp in part if isinstance(comp, Plain))
    if settings.get("interval_method", "random") == "log":
        try:
            base = float(settings.get("log_base", 2.6))
            if base <= 1:
                raise ValueError
            count = (
                len(text.split()) if text.isascii() else sum(c.isalnum() for c in text)
            )
            return random.uniform(
                math.log(count + 1, base), math.log(count + 1, base) + 0.5
            )
        except (ValueError, TypeError):
            return 1.5
    try:
        low, high = [float(v) for v in settings.get("interval", "1.5,3.5").split(",")]
        if low < 0 or high < low:
            raise ValueError
        return random.uniform(low, high)
    except (TypeError, ValueError, AttributeError):
        return random.uniform(1.5, 3.5)
