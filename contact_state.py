"""只记录联系事实，不保存聊天正文或推断对方是否已读。"""

import asyncio
import copy
import time

from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType


class ContactState:
    def __init__(self, load, save, clock=time.time):
        self.load, self.save, self.clock = load, save, clock
        self.lock = asyncio.Lock()
        self.rows = None

    async def _ready(self):
        if self.rows is None:
            self.rows = await self.load("contact_state", {}) or {}

    def _row(self, session):
        return self.rows.setdefault(
            session, {"revision": 0, "unanswered_contacts": 0, "counted_turns": []}
        )

    async def snapshot(self, session):
        async with self.lock:
            await self._ready()
            return copy.deepcopy(self._row(session))

    async def observe(self, event, configured):
        if isinstance(event, CronMessageEvent) or event.get_extra(
            "krypton_private_diagnostic", False
        ):
            return
        if event.get_sender_id() == event.get_self_id():
            return
        session = event.unified_msg_origin
        async with self.lock:
            await self._ready()
            changed = False
            if session in self.rows or session in configured:
                row = self._row(session)
                row["revision"] += 1
                row["last_user_at"] = self.clock()
                row["unanswered_contacts"] = 0
                changed = True
            if event.get_message_type() == MessageType.GROUP_MESSAGE:
                group = MessageSession.from_str(session)
                for owner, row in self.rows.items():
                    target = MessageSession.from_str(owner)
                    if (
                        target.message_type == MessageType.FRIEND_MESSAGE
                        and target.platform_id == group.platform_id
                        and target.session_id == str(event.get_sender_id())
                    ):
                        row["last_seen_group"] = {
                            "session": session,
                            "time": self.clock(),
                        }
                        changed = True
            if changed:
                await self.save("contact_state", self.rows)

    async def delivered(self, turn, target):
        async with self.lock:
            await self._ready()
            row = self._row(turn.owner)
            counted = row["counted_turns"]
            if turn.id not in counted:
                counted.append(turn.id)
                row["counted_turns"] = counted[-100:]
                if row["revision"] == turn.watched[turn.owner]:
                    row["unanswered_contacts"] += 1
                row["last_contact_at"] = self.clock()
            row["last_delivery_at"] = self.clock()
            row["last_target"] = target
            await self.save("contact_state", self.rows)

    async def outcome(self, turn, status, followup=None):
        async with self.lock:
            await self._ready()
            row = self._row(turn.owner)
            row["last_result"] = {
                "status": status,
                "sent_parts": turn.sent_parts,
                "time": self.clock(),
            }
            if turn.invalid:
                row["last_result"]["reason"] = turn.invalid
            if followup is not None:
                row["last_result"]["followup"] = followup
            await self.save("contact_state", self.rows)

    async def facts(self, session):
        row = await self.snapshot(session)
        return {
            key: value
            for key, value in row.items()
            if key not in {"revision", "counted_turns"}
        }
