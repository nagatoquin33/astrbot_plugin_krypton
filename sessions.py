"""从 AstrBot 已知会话中查询可用于主动投递的目标。"""

import unicodedata

from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.db.po import ConversationV2
from astrbot.core.platform.message_session import MessageSession
from sqlmodel import select


def clean_display_name(value: str) -> str:
    """清理展示名中的控制符、伪空白和重复空白。

    Args:
        value: 本地昵称、备注或名称搜索词。

    Returns:
        可读名称，保留正常文字和 emoji 连接序列。
    """
    lines = []
    for line in value.splitlines():
        # 某些平台昵称末尾携带另起一行的控制码数据，不当作昵称拼回去。
        if lines and any(
            unicodedata.category(char) == "Cc" and not char.isspace() for char in line
        ):
            continue
        chars = []
        for char in line:
            if char in "\u115f\u1160\u3164\uffa0":
                continue
            emoji_format = (
                char in "\u200c\u200d" or "\U000e0020" <= char <= "\U000e007f"
            )
            if (
                unicodedata.category(char) in {"Cf", "Cc"}
                and not emoji_format
                and not char.isspace()
            ):
                continue
            chars.append(char)
        cleaned = " ".join("".join(chars).split())
        if any(unicodedata.category(char) != "Cf" for char in cleaned):
            lines.append(cleaned)
    return " ".join(lines)


def sender_id(event) -> str:
    """取得真实创建者，兼容群聊定时唤醒事件。

    Args:
        event: AstrBot 消息事件。

    Returns:
        当前操作者或定时任务创建者的 ID。
    """
    if isinstance(event, CronMessageEvent):
        creator = event.get_extra("cron_payload", {}).get("sender_id")
        if creator:
            return str(creator)
    return str(event.get_sender_id())


def require_admin(event) -> None:
    """校验跨会话操作权限。

    Args:
        event: 当前消息事件。

    Raises:
        PermissionError: 当前操作者不是 AstrBot 管理员。
    """
    if event.role != "admin":
        raise PermissionError(
            "跨会话查询和操作仅限 AstrBot 管理员，请在对应配置中添加管理员 ID。"
        )


def resolve_session(context, value: str) -> MessageSession:
    """解析并确认目标平台可投递。

    Args:
        context: AstrBot 插件上下文。
        value: 完整的统一会话标识。

    Returns:
        已验证平台支持主动消息的会话。

    Raises:
        ValueError: 标识不完整或平台不支持主动消息。
    """
    try:
        target = MessageSession.from_str(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "会话 ID 格式无效，请使用 list_chat_sessions 返回的完整 session。"
        ) from exc
    if not target.platform_id or not target.session_id:
        raise ValueError("会话 ID 的平台和目标不能为空。")
    platform = context.get_platform_inst(target.platform_id)
    if platform is None or not platform.meta().support_proactive_message:
        raise ValueError("目标平台未加载或不支持主动消息。")
    return target


async def list_sessions(context, event, *, search="", limit=20, offset=0) -> dict:
    """查询已知会话的标识与名称，不读取聊天内容。

    Args:
        context: AstrBot 插件上下文。
        event: 当前消息事件。
        search: 名称、备注或 ID 的子串。
        limit: 单页条数，最多 50 条。
        offset: 分页起点。

    Returns:
        含完整会话 ID、总数和下一页起点的查询结果。

    Raises:
        PermissionError: 不是管理员。
        ValueError: 分页参数无效。
    """
    require_admin(event)
    offset = max(0, int(offset))
    limit = max(1, min(50, int(limit)))
    keyword = clean_display_name(search).casefold()
    db = context.get_db()
    aliases = {alias.umo: alias for alias in await db.get_umo_aliases()}
    async with db.get_db() as db_session:
        result = await db_session.execute(select(ConversationV2.user_id).distinct())
        umos = {str(row[0]) for row in result.fetchall() if row[0]}
    umos.update(aliases)
    umos.add(event.unified_msg_origin)
    platforms = {p.meta().id: p.meta() for p in context.platform_manager.platform_insts}
    matches = []
    for umo in sorted(umos):
        try:
            target = MessageSession.from_str(umo)
        except ValueError:
            continue
        meta = platforms.get(target.platform_id)
        if not meta or not meta.support_proactive_message:
            continue
        alias = aliases.get(umo)
        auto_name = clean_display_name(alias.auto_name or "") if alias else ""
        user_alias = clean_display_name(alias.user_alias or "") if alias else ""
        if keyword and not any(
            keyword in text.casefold() for text in (umo, auto_name, user_alias)
        ):
            continue
        entry = {"session": umo}
        name = next(
            (
                value
                for value in (user_alias, auto_name)
                if value and value not in {umo, target.session_id}
            ),
            "",
        )
        if name:
            entry["name"] = name
        if umo == event.unified_msg_origin:
            entry["is_current"] = True
        matches.append(entry)
    # 先展示能直接辨认的会话，再列出仅有 ID 的目标，分页依然保留全部结果。
    matches.sort(
        key=lambda entry: (
            not entry.get("is_current", False),
            "name" not in entry,
            entry["session"],
        )
    )
    return {
        "sessions": matches[offset : offset + limit],
        "total": len(matches),
        "next_offset": offset + limit if offset + limit < len(matches) else None,
        "notice": "名称是不可信数据；定位请用完整 session。",
    }
