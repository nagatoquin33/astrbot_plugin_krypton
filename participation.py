"""用可选 JEV 判断筛选普通群消息，再交给原生主 Agent。"""

import asyncio
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime

from astrbot.api import logger
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from .jev import JevJudgmentError, JevSettings
from .platform_api import physical_chat_id
from .sessions import clean_display_name
from .tasks import proactive_enabled


class ParticipationGate:
    def __init__(self, context, config, judge):
        self.context = context
        self.config = config
        self.judge = judge
        self.messages = OrderedDict()
        self.last_check = {}
        self.last_wakeup = {}
        self.busy = set()
        self.closed = False

    def _settings_for(self, event):
        settings = JevSettings.from_config(self.config)
        if (
            not settings.enabled
            or not settings.api_key
            or not proactive_enabled(self.context, event)
        ):
            return None
        if (
            not self.context.get_config(umo=event.unified_msg_origin)
            .get("provider_settings", {})
            .get("enable", True)
        ):
            return None
        return settings

    def _scope(self, event, settings):
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return None
        session = MessageSession.from_str(event.unified_msg_origin)
        platform = self.context.get_platform_inst(session.platform_id)
        if platform is None:
            return None
        group_id = physical_chat_id(session, platform)
        group_session = f"{session.platform_id}:GroupMessage:{group_id}"
        if (
            event.unified_msg_origin not in settings.group_sessions
            and group_session not in settings.group_sessions
        ):
            return None
        return session.platform_id, group_id

    def _append(self, session, message):
        message["time"] = datetime.now(UTC).isoformat()
        messages = self.messages.setdefault(session, deque(maxlen=10))
        messages.append(message)
        self.messages.move_to_end(session)
        if len(self.messages) > 256:
            self.messages.popitem(last=False)

    async def consider(self, event) -> bool:
        if self.closed:
            return False
        try:
            settings = self._settings_for(event)
            if settings is None:
                return False
            scope = self._scope(event, settings)
            if scope is None or event.get_sender_id() == event.get_self_id():
                return False
            text = event.get_message_outline().strip()[:1000]
            if not text:
                return False
            self._append(
                event.unified_msg_origin,
                {
                    "sender": clean_display_name(event.get_sender_name()) or "群成员",
                    "text": text,
                },
            )
            now = time.monotonic()
            if event.is_at_or_wake_command:
                self.last_wakeup[scope] = now
                return False
            if event.get_extra("handlers_parsed_params") or event._has_send_oper:
                return False
            if scope in self.busy:
                return False
            if (
                now - self.last_check.get(scope, float("-inf"))
                < settings.check_interval
            ):
                return False
            if (
                now - self.last_wakeup.get(scope, float("-inf"))
                < settings.wakeup_cooldown
            ):
                return False
            self.last_check[scope] = now
            self.busy.add(scope)
            try:
                score = await self.judge.score(
                    settings,
                    {
                        "recent_messages": list(
                            self.messages[event.unified_msg_origin]
                        ),
                        "participation_hint": settings.participation_hint,
                        "current_time": datetime.now(UTC).isoformat(),
                    },
                )
            finally:
                self.busy.discard(scope)
            # 等待期间可能发生配置关闭、卸载或一次正常回复。
            current = self._settings_for(event)
            if self.closed or current is None or self._scope(event, current) != scope:
                return False
            if self.last_wakeup.get(scope, float("-inf")) >= now:
                return False
            if score < current.threshold:
                return False
            self.last_wakeup[scope] = time.monotonic()
            event.set_extra("krypton_jev_score", score)
            return True
        except asyncio.CancelledError:
            raise
        except JevJudgmentError as exc:
            logger.warning("JEV 群聊判断跳过：%s", exc)
            return False
        except Exception as exc:
            logger.warning("JEV 群聊判断跳过：%s", type(exc).__name__)
            return False

    def note_sent(self, event):
        settings = JevSettings.from_config(self.config)
        if self.closed or not settings.enabled:
            return
        scope = self._scope(event, settings)
        if scope is not None:
            self.last_wakeup[scope] = time.monotonic()
            result = event.get_result()
            if result is not None:
                text = result.get_plain_text(with_other_comps_mark=True).strip()[:1000]
                if text:
                    self._append(
                        event.unified_msg_origin, {"sender": "助手", "text": text}
                    )

    async def close(self):
        self.closed = True
        await self.judge.close()
