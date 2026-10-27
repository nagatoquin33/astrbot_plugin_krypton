"""按需读取目标聊天近期消息，明确区分平台历史与本地记录。"""

import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import logger
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from .platform_api import OneBotClient, physical_chat_id
from .sessions import clean_display_name, require_admin


def message_text(parts: Any, names: dict[str, str] | None = None) -> str:
    """提取可读文本与媒体占位，不下载媒体或生成未经验证的描述。

    Args:
        parts: OneBot 消息段、CQ 字符串或原生持久化消息段。
        names: 当前消息页中已知的发言人名称，用于展示提及对象。

    Returns:
        文本及简短媒体类型说明。
    """
    labels = {
        "image": "图片",
        "record": "语音",
        "video": "视频",
        "file": "文件",
        "face": "表情",
        "reply": "回复",
        "forward": "合并转发",
        "node": "转发节点",
        "json": "卡片",
        "xml": "卡片",
    }
    names = names or {}
    if isinstance(parts, str):
        # CQ 消息里的媒体地址可能带临时令牌，现场阅读只需要知道消息类型。
        def replace_cq(match):
            kind, data = match.group(1), match.group(2) or ""
            if kind == "at":
                target = re.search(r"(?:^|,)qq=([^,]+)", data)
                ident = target.group(1) if target else ""
                return "@" + (
                    "全体成员" if ident == "all" else names.get(ident, "某人")
                )
            return f"[{labels.get(kind, kind)}]"

        return re.sub(
            r"\[CQ:(\w+)(?:,([^\]]*))?\]",
            replace_cq,
            parts,
        )
    if not isinstance(parts, list):
        return ""
    texts = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type", "unknown")).lower()
        data = part.get("data") if isinstance(part.get("data"), dict) else part
        if kind in {"text", "plain"}:
            texts.append(str(data.get("text") or ""))
        elif kind == "at":
            target = str(data.get("qq") or data.get("user_id") or "")
            name = clean_display_name(str(data.get("name") or ""))
            if not name or name == target:
                name = "全体成员" if target == "all" else names.get(target, "某人")
            texts.append("@" + name)
        else:
            texts.append(f"[{labels.get(kind, kind)}]")
    return " ".join(texts).strip()


async def get_chat_context(
    context: Any,
    event: Any,
    session: str,
    *,
    limit: int = 10,
    client: OneBotClient | None = None,
    activity: Any = None,
) -> dict:
    """读取当前聊天或管理员指定聊天的近期消息。

    Args:
        context: AstrBot 插件上下文。
        event: 当前工具调用者或原生任务唤醒事件。
        session: 完整目标 UMO。
        limit: 返回条数，最多 20。
        client: 插件共用的平台客户端。

    Returns:
        仅含发言人、时间和正文的消息；降级或失败时附简短说明。

    Raises:
        PermissionError: 普通成员试图跨会话读取。
        ValueError: 会话或参数无效。
    """
    session = session.strip()
    if session != event.unified_msg_origin:
        require_admin(event)
    target = MessageSession.from_str(session)
    if not target.platform_id or not target.session_id:
        raise ValueError("会话 ID 的平台和目标不能为空。")
    if target.message_type not in {
        MessageType.GROUP_MESSAGE,
        MessageType.FRIEND_MESSAGE,
    }:
        raise ValueError("只支持读取群聊和私聊消息。")
    limit = max(1, min(20, int(limit)))
    platform = context.get_platform_inst(target.platform_id)
    if platform is None:
        raise ValueError("目标平台未加载。")
    tz_name = context.get_config(umo=event.unified_msg_origin).get("timezone")
    try:
        display_tz = (
            ZoneInfo(tz_name) if tz_name else datetime.now().astimezone().tzinfo
        )
    except ZoneInfoNotFoundError:
        display_tz = UTC
        logger.warning("当前会话时区 %r 无效，近期消息时间按 UTC 显示。", tz_name)
    now = datetime.now(display_tz)
    chat_id = physical_chat_id(target, platform)
    client = client or OneBotClient()
    warnings, messages = [], []
    viewed_records = None
    source = "unavailable"
    if platform.meta().name == "aiocqhttp":
        action = (
            "get_group_msg_history"
            if target.message_type == MessageType.GROUP_MESSAGE
            else "get_friend_msg_history"
        )
        parameter = (
            "group_id"
            if target.message_type == MessageType.GROUP_MESSAGE
            else "user_id"
        )
        try:
            result = await client.call(
                platform, action, **{parameter: chat_id, "count": limit}
            )
            records = result.get("messages") if isinstance(result, dict) else result
            if not isinstance(records, list):
                raise ValueError("平台历史接口未返回消息列表。")
            platform_messages = []
            ordered = sorted(
                (record for record in records if isinstance(record, dict)),
                key=lambda record: (
                    float(record.get("time") or 0),
                    int(record.get("message_seq") or 0),
                ),
            )
            page = []
            names = {}
            for record in ordered[-limit:]:
                sender = record.get("sender") or {}
                user_id = str(sender.get("user_id") or record.get("user_id") or "")
                name = next(
                    (
                        value
                        for raw in (sender.get("card"), sender.get("nickname"))
                        if (value := clean_display_name(str(raw or "")))
                        and value != user_id
                    ),
                    "未知发言人",
                )
                page.append((record, name))
                if user_id:
                    names[user_id] = name
            for record, name in page:
                text = message_text(
                    record.get("message", record.get("raw_message", "")), names
                )
                timestamp = record.get("time")
                platform_messages.append(
                    {
                        "sender": name,
                        "time": datetime.fromtimestamp(float(timestamp), UTC)
                        if timestamp
                        else None,
                        "text": text[:2000] + ("…" if len(text) > 2000 else ""),
                    }
                )
            messages = platform_messages
            source = "platform"
            if target.message_type == MessageType.GROUP_MESSAGE:
                viewed_records = ordered[-limit:]
        except Exception as exc:
            warnings.append(f"{action}: {type(exc).__name__}: {exc}")
    else:
        warnings.append("此平台尚未接入在线历史接口，尝试读取本地记录。")

    if source != "platform":
        cfg = context.get_config(umo=session)
        history_enabled = cfg.get("provider_ltm_settings", {}).get(
            "group_message_history_enable", False
        )
        if target.message_type == MessageType.GROUP_MESSAGE and not history_enabled:
            warnings.append(
                "目标会话的本地群消息记录未开启，不能用旧记录作为现场背景。"
            )
        else:
            try:
                records = await context.message_history_manager.get(
                    platform_id=target.platform_id, user_id=session, page_size=limit
                )
                local_messages = []
                names = {}
                for record in records:
                    name = clean_display_name(str(record.sender_name or ""))
                    names[str(record.sender_id or "")] = (
                        name
                        if name and name != str(record.sender_id or "")
                        else "未知发言人"
                    )
                for record in records:
                    content = record.content or {}
                    text = message_text(content.get("message", []), names)
                    timestamp = record.created_at
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=UTC)
                    local_messages.append(
                        {
                            "sender": names[str(record.sender_id or "")],
                            "time": timestamp.astimezone(UTC),
                            "text": text[:2000] + ("…" if len(text) > 2000 else ""),
                        }
                    )
                messages = local_messages
                source = "local"
            except Exception as exc:
                warnings.append(f"本地消息查询失败：{type(exc).__name__}: {exc}")
    messages.sort(
        key=lambda message: message["time"] or datetime.min.replace(tzinfo=UTC)
    )
    for message in messages:
        if message["time"] is not None:
            timestamp = message["time"].astimezone(display_tz)
            message["time"] = timestamp.strftime(
                "%m-%d %H:%M" if timestamp.year == now.year else "%Y-%m-%d %H:%M"
            )
    if warnings:
        log = (
            logger.warning
            if source == "unavailable" or platform.meta().name == "aiocqhttp"
            else logger.debug
        )
        log("近期消息查询 %s：%s", session, "；".join(warnings))
    if activity is not None and viewed_records:
        try:
            await activity.mark_viewed(event, session, viewed_records)
        except Exception:
            logger.exception("群聊查看基准保存失败")
    result = {"messages": messages[-limit:]}
    if source == "local":
        result["note"] = "仅本地留存记录，可能不完整。"
    elif source == "unavailable":
        result["error"] = "未能获取近期消息，请检查平台历史接口及原生消息记录设置。"
    return result
