"""仅为主动联系任务提供逐段发送、状态复核和送达记账。"""

import asyncio
import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.message.message_event_result import (
    MessageChain,
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.star_handler import EventType

from .contact_segments import build_parts, segment_delay
from .contact_state import ContactState
from .contact_tasks import ContactTaskManager, contact_schedule
from .platform_api import physical_chat_id
from .sessions import sender_id
from .tasks import task_metadata

_DECORATING_CONTACT = ContextVar("krypton_decorating_contact", default=None)
_QUIET_STOP = "当前处于免打扰时段"


class ContactStopped(ValueError):
    """当前主动行动已过期，不能继续发送或自动重试。"""


@dataclass
class ContactTurn:
    owner: str
    event: object
    base_context: object
    managed: bool
    id: str = field(default_factory=lambda: uuid4().hex)
    watched: dict = field(default_factory=dict)
    cache: set = field(default_factory=set)
    lock: object = field(default_factory=asyncio.Lock)
    sent_parts: int = 0
    invalid: str = ""
    cancelled: bool = False
    result: str = "silent"
    actual_texts: list = field(default_factory=list)
    original_texts: list = field(default_factory=list)


class ContactContext(Context):
    def __init__(self, original, runtime, event):
        self._original, self._runtime, self._event = original, runtime, event
        self._cron_manager = ContactTaskManager(original.cron_manager, runtime, event)

    def __getattribute__(self, name):
        if name == "cron_manager":
            return object.__getattribute__(self, "_cron_manager")
        if name in {"_original", "_runtime", "_event", "send_message", "__class__"}:
            return object.__getattribute__(self, name)
        return getattr(object.__getattribute__(self, "_original"), name)

    async def send_message(self, session, message_chain):
        return await self._runtime.send(self._event, str(session), message_chain)


class ContactDeliveryEvent(AstrMessageEvent):
    def __init__(self, runtime, turn, target):
        session = MessageSession.from_str(target)
        platform = runtime.context.get_platform_inst(session.platform_id)
        message = AstrBotMessage()
        message.type = session.message_type
        message.session_id = session.session_id
        recipient = (
            session.session_id
            if session.message_type == MessageType.FRIEND_MESSAGE
            else sender_id(turn.event)
        )
        message.sender = MessageMember(user_id=recipient, nickname="")
        message.message = []
        message.message_id = ""
        message.message_str = ""
        message.raw_message = None
        if session.message_type == MessageType.GROUP_MESSAGE:
            message.group_id = physical_chat_id(session, platform)
        message.self_id = turn.event.get_self_id()
        super().__init__("", message, platform.meta(), session.session_id)
        self._runtime, self._turn, self._target = runtime, turn, target
        self.role = turn.event.role
        self.bot = getattr(platform, "bot", None)
        plugins = runtime.context.get_config(umo=target).get("plugin_set", ["*"])
        self.plugins_name = None if "*" in plugins else plugins
        self.sent_components = []
        self.finished = False
        self.set_extra("krypton_contact_delivery", True)
        self.set_extra("cron_payload", turn.event.get_extra("cron_payload", {}))

    async def send(self, message):
        if self.finished:
            raise ContactStopped("本次装饰发送已经结束")
        await self._runtime.raw_send(self._turn, self._target, message)
        self.sent_components.extend(message.chain)
        self._has_send_oper = True


class ContactRuntime:
    def __init__(
        self,
        context,
        config,
        load,
        save,
        private,
        hook=call_event_hook,
        sleep=asyncio.sleep,
    ):
        self.context, self.config, self.private = context, config, private
        self.state = ContactState(load, save)
        self.hook, self.sleep = hook, sleep
        self.closed = False
        self._router = None
        self._original_send = None
        self._active_dispatches = 0

    def _restore_router(self):
        if self._router is not None and self.context.send_message is self._router:
            self.context.send_message = self._original_send

    def _install_router(self):
        if self._router is not None:
            return
        original = self.context.send_message

        async def scoped_send(session, message_chain):
            delivery = _DECORATING_CONTACT.get()
            if delivery is not None and delivery._runtime is self:
                if str(session) != delivery._target:
                    # 其他目标可能是输出插件自己的管理通知，不记作本次主动联系。
                    return await original(session, message_chain)
                await delivery.send(message_chain)
                return True
            return await original(session, message_chain)

        self._original_send, self._router = original, scoped_send
        self.context.send_message = scoped_send

    async def _dispatch(self, delivery, hook_type):
        self._active_dispatches += 1
        token = _DECORATING_CONTACT.set(delivery)
        try:
            return await self.hook(delivery, hook_type)
        finally:
            _DECORATING_CONTACT.reset(token)
            self._active_dispatches -= 1
            if self.closed and not self._active_dispatches:
                self._restore_router()

    async def initialize(self):
        async with self.state.lock:
            await self.state._ready()

    async def observe(self, event):
        if self.state.rows is None and not self.config.get(
            "enable_private_proactive", False
        ):
            return
        configured = (
            self.private._targets()
            if self.config.get("enable_private_proactive", False)
            else {}
        )
        await self.state.observe(event, configured)

    async def begin(self, event, run_context):
        if (
            self.closed
            or not isinstance(event, CronMessageEvent)
            or event.get_extra("krypton_contact_turn")
        ):
            return
        metadata = task_metadata(event.get_extra("cron_payload", {}) or {})
        if metadata.get("intent") != "contact" and not metadata.get(
            "private_bootstrap"
        ):
            return
        # 群聊定时事件原本以群号充当发言人，原生任务工具需要真实创建者做鉴权。
        creator = event.get_extra("cron_payload", {}).get("sender_id")
        if creator:
            event.message_obj.sender.user_id = str(creator)
        owner = metadata.get("contact_session") or event.unified_msg_origin
        turn = ContactTurn(
            owner,
            event,
            run_context.context.context,
            bool(metadata.get("managed_private") or metadata.get("private_bootstrap")),
        )
        latest_input = 0
        for session in {owner, event.unified_msg_origin}:
            snapshot = await self.state.snapshot(session)
            turn.watched[session] = snapshot["revision"]
            latest_input = max(latest_input, snapshot.get("last_user_at", 0))
        started = event.get_extra("cron_job", {}).get("run_started_at")
        if started:
            try:
                if latest_input > datetime.fromisoformat(started).timestamp():
                    turn.invalid = "任务启动期间已收到新消息，旧主动行动停止"
            except (ValueError, TypeError):
                pass
        turn.original_texts = list(
            event.get_extra("_send_message_to_user_current_session_plain_texts", [])
            or []
        )
        event.set_extra("krypton_contact_turn", turn)
        self._install_router()
        proxy = ContactContext(turn.base_context, self, event)
        run_context.context.context = proxy
        event.context_obj = proxy

    async def context_viewed(self, event, session):
        turn = event.get_extra("krypton_contact_turn")
        if turn and session not in {turn.owner, event.unified_msg_origin}:
            turn.watched[session] = (await self.state.snapshot(session))["revision"]

    async def guard(self, turn, *, for_task=False):
        if turn.cancelled or asyncio.current_task().cancelling():
            turn.cancelled = True
            raise asyncio.CancelledError()
        if not turn.invalid or (for_task and turn.invalid == _QUIET_STOP):
            if self.closed:
                turn.invalid = "插件已卸载"
            elif (
                not self.context.get_config(umo=turn.owner)
                .get("provider_settings", {})
                .get("enable", True)
            ):
                turn.invalid = "目标会话 AI 已关闭"
            elif (
                not self.context.get_config(umo=turn.owner)
                .get("provider_settings", {})
                .get("proactive_capability", {})
                .get("add_cron_tools", True)
            ):
                turn.invalid = "主动型能力已关闭"
            elif turn.managed and not self.private._allowed(
                turn.owner, sender_id(turn.event)
            ):
                turn.invalid = "目标已关闭主动联系或移出允许范围"
            elif (
                not for_task
                and turn.managed
                and self.private._outside_quiet(turn.owner, self.state.clock())
                > self.state.clock()
            ):
                turn.invalid = _QUIET_STOP
            else:
                for session, revision in turn.watched.items():
                    if (await self.state.snapshot(session))["revision"] != revision:
                        turn.invalid = "已有新消息，旧主动行动停止"
                        break
        if turn.invalid and not (for_task and turn.invalid == _QUIET_STOP):
            if turn.result != "unknown":
                turn.result = "partial" if turn.sent_parts else "cancelled"
            await self.state.outcome(turn, turn.result)
            raise ContactStopped(turn.invalid)

    async def raw_send(self, turn, target, chain):
        await self.guard(turn)
        if target != turn.event.unified_msg_origin and turn.event.role != "admin":
            raise PermissionError("跨会话发送仅限管理员。")
        try:
            token = _DECORATING_CONTACT.set(None)
            try:
                sent = await turn.base_context.send_message(target, chain)
            finally:
                _DECORATING_CONTACT.reset(token)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                turn.cancelled = True
            turn.result, turn.invalid = "unknown", "发送结果未知，本轮不自动重发"
            await self.state.outcome(turn, "unknown")
            raise
        if sent is not True:
            turn.invalid = "平台未确认发送成功，本轮停止"
            raise ContactStopped(turn.invalid)
        turn.sent_parts += 1
        turn.event._has_send_oper = True
        turn.result = "sent"
        if target == turn.event.unified_msg_origin:
            text = chain.get_plain_text().strip()
            if text:
                turn.actual_texts.append(text)
        try:
            await self.state.delivered(turn, target)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                turn.cancelled = True
            turn.invalid = "已有确认发送，但记账失败，本轮停止且不重发"
            turn.result = "partial"
            raise

    async def send(self, event, target, chain):
        turn = event.get_extra("krypton_contact_turn")
        if not turn:
            raise ContactStopped("缺少主动行动上下文")
        fingerprint = hashlib.sha256(
            (
                target
                + json.dumps(
                    [c.toDict() for c in chain.chain], sort_keys=True, default=str
                )
            ).encode()
        ).hexdigest()
        async with turn.lock:
            await self.guard(turn)
            if fingerprint in turn.cache:
                return True
            if target not in turn.watched:
                turn.watched[target] = (await self.state.snapshot(target))["revision"]
            if (
                self.context.get_platform_inst(
                    MessageSession.from_str(target).platform_id
                )
                is None
            ):
                turn.invalid = "目标平台未加载"
                turn.result = "failed"
                await self.state.outcome(turn, "failed")
                raise ContactStopped(turn.invalid)
            delivery = ContactDeliveryEvent(self, turn, target)
            result = MessageEventResult(chain=list(chain.chain))
            result.result_content_type = ResultContentType.LLM_RESULT
            delivery.set_result(result)
            before = turn.sent_parts
            try:
                await self._dispatch(delivery, EventType.OnDecoratingResultEvent)
                await self.guard(turn)
                result = delivery.get_result()
                if not delivery.is_stopped() and result is not None:
                    settings = (
                        self.context.get_config(umo=target)
                        .get("platform_settings", {})
                        .get("segmented_reply", {})
                    )
                    parts = build_parts(
                        result.chain,
                        settings,
                        self.config.get("contact_segmented_reply", True)
                        and turn.sent_parts == before,
                        result.is_model_result(),
                    )
                    for index, part in enumerate(parts):
                        if index:
                            await self.sleep(segment_delay(parts[index - 1], settings))
                        await delivery.send(MessageChain(chain=part))
                if turn.sent_parts == before:
                    turn.invalid = "发送被装饰钩子终止或结果为空"
                    raise ContactStopped(turn.invalid)
                turn.cache.add(fingerprint)
                return True
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    turn.cancelled = True
                if not turn.invalid:
                    turn.invalid = "本轮发送已中止，不自动重发已确认的内容"
                if turn.result != "unknown":
                    turn.result = "partial" if turn.sent_parts > before else "cancelled"
                raise
            finally:
                try:
                    if delivery.sent_components and not self.closed:
                        sent_result = MessageEventResult(chain=delivery.sent_components)
                        sent_result.result_content_type = ResultContentType.LLM_RESULT
                        delivery.set_result(sent_result)
                        await self._dispatch(
                            delivery, EventType.OnAfterMessageSentEvent
                        )
                finally:
                    delivery.finished = True
                    await self.state.outcome(turn, turn.result)

    def reconcile_history(self, event):
        turn = event.get_extra("krypton_contact_turn")
        if turn:
            event.set_extra(
                "_send_message_to_user_current_session_plain_texts",
                [*turn.original_texts, *turn.actual_texts],
            )

    async def finish(self, event):
        turn = event.get_extra("krypton_contact_turn")
        if turn and not self.closed:
            self.reconcile_history(event)
            followup = await contact_schedule(
                self.context, turn.owner, event.get_extra("cron_job", {}).get("id", "")
            )
            await self.state.outcome(turn, turn.result, followup=followup)
            logger.info(
                "[氪联系] %s 本轮结果：%s，确认 %d 段；后续联系：%s；停止原因：%s",
                turn.owner,
                turn.result,
                turn.sent_parts,
                followup["state"],
                turn.invalid or "无",
            )

    async def close(self):
        self.closed = True
        if not self._active_dispatches:
            self._restore_router()
