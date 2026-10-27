"""按管理员上次查看的群消息计算可见增量。"""

import asyncio
import time

from astrbot.api import logger
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from .platform_api import physical_chat_id
from .sessions import list_sessions, require_admin, sender_id


def activity_sort_key(item: dict) -> tuple:
    """先看有更新的群，再看增量未知的群，最后是已看完和不可用的群。"""
    new = item.get("new_messages")
    recent = item.get("recent_messages_visible")
    if new is not None and new > 0:
        priority, count = 0, new
    elif new is None and recent is not None:
        priority, count = 1, recent
    elif new == 0:
        priority, count = 2, 0
    else:
        priority, count = 3, 0
    return priority, -count, -(recent or 0), item["session"]


class GroupActivity:
    def __init__(self, context, client, load, save):
        self.context = context
        self.client = client
        self.load = load
        self.save = save
        self.lock = asyncio.Lock()

    async def mark_viewed(self, event, session, records):
        if event.role != "admin" or not records:
            return
        latest = records[-1]
        message_id = latest.get("message_id")
        if message_id is None:
            return
        marker = {
            "id": str(message_id),
            "time": float(latest.get("time") or 0),
            "seq": int(latest.get("message_seq") or 0),
        }
        key = f"{sender_id(event)}\n{session}"
        async with self.lock:
            markers = await self.load("group_activity_markers", {}) or {}
            previous = markers.get(key)
            if isinstance(previous, dict) and previous.get("time") and marker["time"]:
                old_order = (previous["time"], previous.get("seq", 0))
                new_order = (marker["time"], marker["seq"])
                if new_order < old_order:
                    return
            markers[key] = marker
            await self.save("group_activity_markers", markers)

    async def list_activity(self, event, limit=20, offset=0, hours=24):
        require_admin(event)
        limit = max(1, min(50, int(limit)))
        offset = max(0, int(offset))
        hours = max(1, min(168, int(hours)))
        cutoff = time.time() - hours * 3600
        sessions = []
        page_offset = 0
        while True:
            page = await list_sessions(
                self.context, event, limit=50, offset=page_offset
            )
            for entry in page["sessions"]:
                if (
                    MessageSession.from_str(entry["session"]).message_type
                    == MessageType.GROUP_MESSAGE
                ):
                    sessions.append(entry)
            if page["next_offset"] is None:
                break
            page_offset = page["next_offset"]
        async with self.lock:
            markers = dict(await self.load("group_activity_markers", {}) or {})
        semaphore = asyncio.Semaphore(5)
        history_tasks = {}

        async def load_history(platform, chat_id):
            async with semaphore:
                response = await self.client.call(
                    platform, "get_group_msg_history", group_id=chat_id, count=100
                )
            records = (
                response.get("messages") if isinstance(response, dict) else response
            )
            if not isinstance(records, list):
                raise ValueError("平台未返回消息列表")
            return sorted(
                (r for r in records if isinstance(r, dict)),
                key=lambda r: (
                    float(r.get("time") or 0),
                    int(r.get("message_seq") or 0),
                ),
            )[-100:]

        async def inspect(entry):
            session = entry["session"]
            result = {k: v for k, v in entry.items() if k in {"session", "name"}}
            marker = markers.get(f"{sender_id(event)}\n{session}")
            target = MessageSession.from_str(session)
            platform = self.context.get_platform_inst(target.platform_id)
            try:
                chat_id = physical_chat_id(target, platform)
                key = (target.platform_id, chat_id)
                # 共享物理群的只读查询，各会话仍单独计算自己的增量。
                if key not in history_tasks:
                    history_tasks[key] = asyncio.create_task(
                        load_history(platform, chat_id)
                    )
                ordered = await history_tasks[key]
                timestamps = [float(r.get("time") or 0) for r in ordered]
                if not timestamps or any(t > 0 for t in timestamps):
                    result["recent_messages_visible"] = sum(
                        t >= cutoff for t in timestamps if t > 0
                    )
                if any(t <= 0 for t in timestamps):
                    result["recent_note"] = "消息缺少时间，无法计入近期数量。"
                if marker is None:
                    return {**result, "status": "尚未查看，暂无基准"}
                marker_id = marker.get("id") if isinstance(marker, dict) else marker
                indexes = [
                    i
                    for i, r in enumerate(ordered)
                    if str(r.get("message_id")) == marker_id
                ]
                if indexes:
                    return {**result, "new_messages": len(ordered) - indexes[-1] - 1}
                return {**result, "status": "基准消息不在平台返回的历史中，数量未知"}
            except Exception as exc:
                logger.warning(
                    "群活动查询 %s 失败：%s: %s", session, type(exc).__name__, exc
                )
                return {**result, "status": "群消息历史不可用，数量未知"}

        groups = await asyncio.gather(*(inspect(entry) for entry in sessions))
        groups.sort(key=activity_sort_key)
        return {
            "groups": groups[offset : offset + limit],
            "total": len(sessions),
            "next_offset": offset + limit if offset + limit < len(sessions) else None,
            "recent_hours": hours,
            "notice": "近期数量是平台返回记录中的可见数量，可能因历史保留或返回条数限制偏少；增量仅在基准消息可见时计算。查看群内容后更新基准。未知不代表没有消息。",
        }
