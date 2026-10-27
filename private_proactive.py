"""为手动指定的 QQ 在缺少未来任务时提供一次启动机会。"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.db.po import CronJob
from astrbot.core.platform.message_type import MessageType

from .tasks import task_metadata

DEFAULT_GOAL = (
    "当前目标私聊没有待执行的相关未来任务，本轮唤醒用于建立你自己的后续日程。"
    "先用 future_task_plus 确认相关任务；如果已经有人安排则复用，不重复创建。"
    "结合人设与当前私聊上下文，自己决定下次何时联系对方、聊什么或办什么事，"
    "并实际调用 future_task_plus 创建未来任务，不要只口头说已安排，"
    "也不要要求对方提醒你或替你创建任务。若对方明确要求停止主动联系，则遵守。"
    "必要时可先调用其他工具了解情况，本轮不必立即发送私聊消息。"
    "对方一直没有回复时，可以先查看群活动，再按需读取群聊现场；群活跃不代表对方在线。"
    "只有需要向对方表达或分享的内容才发送，不要发送本轮检查报告。"
)
OWNER_KEY = "krypton_private_bootstrap"
STATE_KEY = "private_proactive_state"


class PrivateProactive:
    def __init__(self, context, config, load, save, clock=time.time):
        self.context = context
        self.config = config
        self.load, self.save, self.clock = load, save, clock
        self.lock = asyncio.Lock()
        self.states = {}
        self.jobs = {}
        self.running = {}
        self.started = False
        self.closed = False

    def _targets(self):
        raw_ids = self.config.get("private_proactive_qq_ids", []) or []
        if not isinstance(raw_ids, list):
            return {}
        ids = {
            str(int(text))
            for raw in raw_ids
            if (text := str(raw).strip()).isascii() and text.isdigit() and int(text) > 0
        }
        platform_id = str(
            self.config.get("private_proactive_platform_id", "") or ""
        ).strip()
        platforms = [
            p
            for p in self.context.platform_manager.platform_insts
            if p.meta().name == "aiocqhttp"
            and p.meta().support_proactive_message
            and (not platform_id or p.meta().id == platform_id)
        ]
        if len(platforms) != 1:
            return {}
        platform_id = platforms[0].meta().id
        return {f"{platform_id}:FriendMessage:{qq}": qq for qq in sorted(ids)}

    def _allowed(self, session, sender):
        return self._blocked_reason(session, sender) is None

    def _blocked_reason(self, session, sender):
        if self.closed:
            return "插件已卸载"
        if not self.config.get("enable_private_proactive", False):
            return "未开启私聊主动联系"
        if self._targets().get(session) != str(sender):
            return "QQ 号或 OneBot 账号未匹配"
        cfg = self.context.get_config(umo=session)
        plugins = cfg.get("plugin_set", ["*"])
        provider = cfg.get("provider_settings", {})
        if "*" not in plugins and "astrbot_plugin_krypton" not in plugins:
            return "目标配置未启用氪插件"
        if not provider.get("enable", True):
            return "目标配置未启用 AI"
        if not provider.get("proactive_capability", {}).get("add_cron_tools", True):
            return "目标配置未启用原生主动型能力"
        return None

    async def _has_future_task(self, session):
        return await self._find_future_task(session) is not None

    async def _find_future_task(self, session):
        manager = self.context.cron_manager
        for job in await manager.list_jobs("active_agent"):
            payload = job.payload or {}
            if session not in (
                payload.get("session"),
                task_metadata(payload).get("source_session"),
            ):
                continue
            if job.status == "running":
                return job
            if not job.enabled:
                continue
            next_run = manager.get_next_run_time(job.job_id)
            if next_run is not None and next_run.timestamp() >= self.clock():
                return job
        return None

    def _deadline(self, session, after):
        delay = (
            max(1, float(self.config.get("private_proactive_idle_minutes", 30))) * 60
        )
        return self._outside_quiet(session, after + delay)

    def _outside_quiet(self, session, deadline):
        quiet = str(self.config.get("private_proactive_quiet_hours", "") or "").strip()
        if not quiet:
            return deadline
        start, end = [
            datetime.strptime(t.strip(), "%H:%M").time() for t in quiet.split("-", 1)
        ]
        cfg = self.context.get_config(umo=session)
        tz = ZoneInfo(cfg.get("timezone") or "UTC")
        local = datetime.fromtimestamp(deadline, UTC).astimezone(tz)
        current = local.time()
        in_quiet = (
            start <= current < end if start < end else current >= start or current < end
        )
        if start != end and in_quiet:
            day = local.date() + (
                timedelta(days=1) if start > end and current >= start else timedelta()
            )
            deadline = datetime.combine(day, end, tzinfo=tz).timestamp()
        return deadline

    def _new_state(self, session, sender):
        now = self.clock()
        return {
            "sender_id": str(sender),
            "last_activity": now,
            "deadline": self._deadline(session, now),
            "token": uuid4().hex,
            "consumed": False,
            "status": "waiting",
        }

    async def _schedule(self, session, state):
        manager = self.context.cron_manager
        old = self.jobs.pop(session, None)
        if old:
            await manager.delete_job(old)
        payload = {
            OWNER_KEY: True,
            "session": session,
            "token": state["token"],
            "run_at": datetime.fromtimestamp(state["deadline"], UTC).isoformat(),
        }
        job = await manager.add_basic_job(
            name="氪：私聊自主行动启动",
            cron_expression="0 0 1 1 *",
            handler=self._run,
            payload=payload,
            enabled=False,
            persistent=False,
            description="目标没有相关未来任务时，唤醒主 Agent 自主创建后续日程。",
        )
        try:
            await manager.update_job(job.job_id, run_once=True, enabled=True)
        except BaseException:
            await manager.delete_job(job.job_id)
            raise
        self.jobs[session] = job.job_id
        state["status"] = "waiting"
        logger.info(
            "[氪私聊] %s 已安排等待，预计唤醒时间：%s", session, payload["run_at"]
        )

    async def start(self):
        async with self.lock:
            if self.started or self.closed:
                return
            manager = self.context.cron_manager
            if manager is None:
                return
            await manager.start(self.context)
            self.states = await self.load(STATE_KEY, {}) or {}
            # 原生不恢复无处理器的维护任务，重新按持久化状态注册本插件的一次性处理器。
            for job in await manager.list_jobs("basic"):
                if job.payload.get(OWNER_KEY) is True:
                    await manager.delete_job(job.job_id)
            if self.config.get("enable_private_proactive", False):
                targets = self._targets()
                if self.config.get("private_proactive_qq_ids") and not targets:
                    logger.warning(
                        "[氪私聊] 未确定有效目标：请填写 QQ 号，并在多个 OneBot 账号时指定平台 ID。"
                    )
                elif not targets:
                    logger.info("[氪私聊] 未填写目标 QQ，不安排唤醒。")
                self.states = {
                    session: state
                    for session, state in self.states.items()
                    if session in targets
                }
                for session, sender in targets.items():
                    reason = self._blocked_reason(session, sender)
                    if reason:
                        logger.info("[氪私聊] %s 跳过：%s", session, reason)
                    if session not in self.states and self._allowed(session, sender):
                        self.states[session] = self._new_state(session, sender)
                for session, state in self.states.items():
                    if not state["consumed"] and self._allowed(
                        session, state["sender_id"]
                    ):
                        if await self._has_future_task(session):
                            state["consumed"] = True
                            state["status"] = "existing_task"
                            logger.info(
                                "[氪私聊] %s 已有相关未来任务，不追加唤醒。", session
                            )
                            continue
                        # 从原等待起点重算，让修改后的等待时间和免打扰配置立即生效。
                        state["deadline"] = self._deadline(
                            session, state["last_activity"]
                        )
                        if state["deadline"] <= self.clock():
                            state["deadline"] = self._deadline(session, self.clock())
                        await self._schedule(session, state)
                    elif state["consumed"]:
                        logger.info(
                            "[氪私聊] %s 本轮已处理，不自动重放；可用 /氪状态 查看。",
                            session,
                        )
                await self.save(STATE_KEY, self.states)
            else:
                logger.info("[氪私聊] 私聊主动联系未启用。")
            self.started = True

    async def on_message(self, event):
        if (
            isinstance(event, CronMessageEvent)
            or event.get_extra("krypton_private_diagnostic", False)
            or event.get_message_type() != MessageType.FRIEND_MESSAGE
        ):
            return
        sender = str(event.get_sender_id())
        if (
            not sender
            or sender == event.get_self_id()
            or not self._allowed(event.unified_msg_origin, sender)
        ):
            return
        await self.start()
        async with self.lock:
            if self.closed:
                return
            session = event.unified_msg_origin
            running = self.running.get(session)
            if running and running is not asyncio.current_task():
                running.cancel()
            state = self._new_state(session, sender)
            self.states[session] = state
            if await self._has_future_task(session):
                state["consumed"] = True
                state["status"] = "existing_task"
                old = self.jobs.pop(session, None)
                if old:
                    await self.context.cron_manager.delete_job(old)
                await self.save(STATE_KEY, self.states)
                logger.info("[氪私聊] %s 已有相关未来任务，不追加唤醒。", session)
                return
            await self.save(STATE_KEY, self.states)
            await self._schedule(session, state)

    async def on_reply(self, event):
        if isinstance(event, CronMessageEvent):
            return
        session = event.unified_msg_origin
        state = self.states.get(session)
        if state is not None and not state["consumed"]:
            await self.on_message(event)

    async def _run(self, session, token, **kwargs):
        current = asyncio.current_task()
        async with self.lock:
            state = self.states.get(session)
            if (
                state is None
                or state["token"] != token
                or state["consumed"]
                or not self._allowed(session, state["sender_id"])
            ):
                return
            if await self._has_future_task(session):
                state["consumed"] = True
                state["status"] = "existing_task"
                self.jobs.pop(session, None)
                await self.save(STATE_KEY, self.states)
                logger.info("[氪私聊] %s 等待期间已建立未来任务，本次不唤醒。", session)
                return
            allowed_at = self._outside_quiet(session, self.clock())
            if allowed_at > self.clock():
                state["deadline"] = allowed_at
                await self.save(STATE_KEY, self.states)
                await self._schedule(session, state)
                logger.info("[氪私聊] %s 处于免打扰时段，已推迟。", session)
                return
            # 先落下已启动状态，异常或发送结果未知时不自动重放本轮。
            state["consumed"] = True
            state["status"] = "running"
            await self.save(STATE_KEY, self.states)
            job_id = self.jobs.pop(session, None)
            self.running[session] = current
            goal = (
                str(self.config.get("private_proactive_goal", "") or "").strip()
                or DEFAULT_GOAL
            )
            note = (
                goal
                + "\n\n本轮等待起点（配置启动或最近一次私聊活动）："
                + datetime.fromtimestamp(state["last_activity"], UTC).isoformat()
            )
            job = CronJob(
                job_id=job_id or uuid4().hex,
                name="私聊自主行动机会",
                job_type="active_agent",
                run_once=True,
                payload={
                    "session": session,
                    "sender_id": state["sender_id"],
                    "origin": "tool",
                    "note": note,
                    "krypton": {"private_bootstrap": True},
                },
            )
        try:
            logger.info("[氪私聊] %s 开始唤醒主 Agent，建立后续日程。", session)
            # 复用本体构建主 Agent、加载目标历史、调用工具与保存结果的完整链路。
            await self.context.cron_manager._run_active_agent_job(
                job, start_time=datetime.now(UTC)
            )
            has_followup = await self._has_future_task(session)
            await self._record_result(
                session, state, "completed" if has_followup else "no_followup"
            )
            logger.info(
                "[氪私聊] %s 唤醒结束：%s",
                session,
                "已有后续未来任务"
                if has_followup
                else "未发现后续未来任务；本轮不会自动重试",
            )
        except asyncio.CancelledError:
            await self._record_result(session, state, "cancelled")
            logger.info("[氪私聊] %s 本轮唤醒已取消。", session)
            raise
        except Exception as exc:
            await self._record_result(session, state, "failed", type(exc).__name__)
            logger.warning("[氪私聊] %s 唤醒失败：%s", session, type(exc).__name__)
            raise
        finally:
            if self.running.get(session) is current:
                self.running.pop(session, None)

    async def _record_result(self, session, state, status, error=None):
        async with self.lock:
            if self.states.get(session) is state:
                state["status"] = status
                state["last_error"] = error
                await self.save(STATE_KEY, self.states)

    async def get_status(self):
        async with self.lock:
            targets = self._targets()
            rows = []
            manager = self.context.cron_manager
            for session, qq in targets.items():
                state = self.states.get(session)
                future = await self._find_future_task(session) if manager else None
                timer = self.jobs.get(session)
                next_run = (
                    manager.get_next_run_time(timer) if timer and manager else None
                )
                future_run = (
                    manager.get_next_run_time(future.job_id)
                    if future and manager
                    else None
                )
                rows.append(
                    {
                        "qq": qq,
                        "session": session,
                        "blocked": self._blocked_reason(session, qq),
                        "timezone": self.context.get_config(umo=session).get("timezone")
                        or "UTC",
                        "status": state.get(
                            "status", "consumed" if state["consumed"] else "waiting"
                        )
                        if state
                        else "uninitialized",
                        "next_run": next_run.timestamp() if next_run else None,
                        "last_error": state.get("last_error") if state else None,
                        "future": {
                            "id": future.job_id,
                            "name": future.name,
                            "next_run": future_run.timestamp() if future_run else None,
                        }
                        if future
                        else None,
                    }
                )
            return {
                "enabled": bool(self.config.get("enable_private_proactive", False)),
                "started": self.started,
                "targets": rows,
                "has_qq_ids": bool(self.config.get("private_proactive_qq_ids")),
            }

    async def reset_wait(self, qq=""):
        if not self.config.get("enable_private_proactive", False):
            raise ValueError("请先开启私聊主动联系。")
        await self.start()
        async with self.lock:
            if self.closed or not self.started:
                raise ValueError("私聊启动器或原生调度器尚未就绪。")
            targets = self._targets()
            selected = [
                (session, sender)
                for session, sender in targets.items()
                if not qq or sender == qq.strip()
            ]
            if len(selected) != 1:
                raise ValueError("请指定已配置的 QQ 号；只有一个目标时可省略。")
            session, sender = selected[0]
            reason = self._blocked_reason(session, sender)
            if reason:
                raise ValueError(reason)
            if session in self.running:
                raise ValueError("该目标正在唤醒中，请等本轮结束。")
            if await self._has_future_task(session):
                raise ValueError(
                    "目标已有相关未来任务，不追加唤醒；请查看未来任务列表。"
                )
            state = self._new_state(session, sender)
            self.states[session] = state
            await self.save(STATE_KEY, self.states)
            await self._schedule(session, state)
            return {
                "qq": sender,
                "deadline": state["deadline"],
                "timezone": self.context.get_config(umo=session).get("timezone")
                or "UTC",
            }

    async def close(self):
        async with self.lock:
            self.closed = True
            for job_id in self.jobs.values():
                await self.context.cron_manager.delete_job(job_id)
            self.jobs.clear()
            tasks = [
                task
                for task in self.running.values()
                if task is not asyncio.current_task()
            ]
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
