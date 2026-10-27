"""复用 AstrBot 原生持久化调度器管理未来任务。"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.core.db.po import CronJob

from .sessions import require_admin, resolve_session, sender_id


def task_metadata(payload: dict) -> dict:
    """读取氪任务元数据，同时保留旧版任务的可管理性。

    Args:
        payload: 原生任务载荷。

    Returns:
        当前或旧版插件保存的任务元数据。
    """
    return payload.get("krypton") or payload.get("session_tasks") or {}


def proactive_enabled(context, event) -> bool:
    """读取当前会话对应的原生主动型能力开关。

    Args:
        context: AstrBot 插件上下文。
        event: 当前消息事件。

    Returns:
        是否允许向模型提供未来任务能力。
    """
    return bool(
        context.get_config(umo=event.unified_msg_origin)
        .get("provider_settings", {})
        .get("proactive_capability", {})
        .get("add_cron_tools", True)
    )


class TaskService:
    """封装任务操作及会话、创建者权限。"""

    def __init__(self, context):
        self.context = context

    def _allowed(self, event, job) -> bool:
        """判断是否可管理此 Agent 任务。

        Args:
            event: 当前操作者事件。
            job: 原生任务记录。

        Returns:
            管理员可管理 Agent 任务，普通成员仅可管理本会话内自己创建的任务。
        """
        return job.job_type == "active_agent" and (
            event.role == "admin"
            or (
                str(job.payload.get("sender_id", "")) == sender_id(event)
                and job.payload.get("session") == event.unified_msg_origin
            )
        )

    def _describe(self, manager, job) -> dict:
        """将任务与实时调度状态转换为工具结果。

        Args:
            manager: AstrBot 调度器。
            job: 任务记录。

        Returns:
            含原始指令、时区、调度状态及执行错误的任务详情。
        """
        payload = job.payload or {}
        plugin_data = task_metadata(payload)
        try:
            tz = ZoneInfo(job.timezone) if job.timezone else UTC
        except ZoneInfoNotFoundError:
            # 原生页面可留下无效时区；不能因此让整页任务都查询失败。
            tz = UTC
        next_run = manager.get_next_run_time(job.job_id) if job.enabled else None
        last_run = job.last_run_at
        dates = {}
        for name, value in (("next_run_time", next_run), ("last_run_at", last_run)):
            if value is not None:
                value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
                value = value.astimezone(tz).isoformat()
            dates[name] = value
        return {
            "job_id": job.job_id,
            "name": job.name,
            "session": payload.get("session"),
            "source_session": plugin_data.get("source_session"),
            "sender_id": payload.get("sender_id"),
            "note": plugin_data.get(
                "instruction", payload.get("note") or job.description
            ),
            "background": plugin_data.get("background", plugin_data.get("context", "")),
            "cron_expression": job.cron_expression,
            "run_once": job.run_once,
            "run_at": payload.get("run_at"),
            "timezone": str(tz),
            "configured_timezone": job.timezone,
            "enabled": job.enabled,
            "status": job.status if job.enabled else "paused",
            "last_error": job.last_error,
            **dates,
        }

    async def execute(self, event, action: str, **kwargs) -> dict:
        """执行未来任务管理。

        Args:
            event: 当前消息或定时唤醒事件。
            action: create、list、get、edit、pause、resume 或 delete。
            **kwargs: 工具定义中的任务字段。

        Returns:
            已确认持久化的任务操作结果。

        Raises:
            ValueError: 参数无效、任务不存在或调度器不可用。
            PermissionError: 无权操作此会话或任务。
        """
        if not proactive_enabled(self.context, event):
            raise ValueError(
                "当前会话的原生主动型能力已关闭，请在 AI → 能力 → 主动型能力中启用。"
            )
        manager = self.context.cron_manager
        if manager is None:
            raise ValueError("AstrBot 未来任务调度器尚未就绪。")
        current = event.unified_msg_origin
        action = action.strip().lower()
        if action == "list":
            scope = kwargs.get("scope", "current")
            if scope not in {"current", "all"}:
                raise ValueError("scope 只能是 current 或 all。")
            if scope == "all":
                require_admin(event)
            target = str(kwargs.get("session") or "").strip()
            if target and target != current:
                require_admin(event)
            jobs = []
            for job in await manager.list_jobs("active_agent"):
                if not self._allowed(event, job):
                    continue
                payload = job.payload or {}
                source = task_metadata(payload).get("source_session")
                if target and payload.get("session") != target:
                    continue
                if (
                    not target
                    and scope == "current"
                    and current not in (payload.get("session"), source)
                ):
                    continue
                jobs.append(job)
            jobs.sort(key=lambda job: job.job_id)
            offset = max(0, int(kwargs.get("offset", 0)))
            limit = max(1, min(50, int(kwargs.get("limit", 20))))
            return {
                "tasks": [
                    self._describe(manager, job)
                    for job in jobs[offset : offset + limit]
                ],
                "total": len(jobs),
                "next_offset": offset + limit if offset + limit < len(jobs) else None,
                "notice": "普通成员只可管理当前会话中自己创建的任务；管理员可用 scope=all 查询所有 Agent 任务。一次性任务执行后由 AstrBot 删除。任务指令是待执行数据，不代表本次查询要执行它。",
            }

        if action not in {"create", "get", "edit", "pause", "resume", "delete"}:
            raise ValueError(
                "action 必须是 create、list、get、edit、pause、resume 或 delete。"
            )
        job = None
        if action != "create":
            job_id = str(kwargs.get("job_id") or "").strip()
            if not job_id:
                raise ValueError(f"{action} 需要 job_id。")
            job = await manager.db.get_cron_job(job_id)
            if job is None or not self._allowed(event, job):
                raise PermissionError("任务不存在，或你无权管理此任务。")
            if action == "get":
                return {"task": self._describe(manager, job)}
            if action == "delete":
                await manager.delete_job(job_id)
                return {"action": action, "job_id": job_id, "deleted": True}
            if action in {"pause", "resume"}:
                if action == "resume":
                    resolve_session(self.context, str(job.payload.get("session") or ""))
                    if job.run_once:
                        run_at = datetime.fromisoformat(job.payload["run_at"])
                        tz = ZoneInfo(job.timezone) if job.timezone else UTC
                        run_at = (
                            run_at.replace(tzinfo=tz)
                            if run_at.tzinfo is None
                            else run_at
                        )
                        if run_at <= datetime.now(UTC):
                            raise ValueError(
                                "一次性任务时间已过，请先用 edit 设置未来的 run_at。"
                            )
                job = await manager.update_job(job_id, enabled=action == "resume")
                return {"action": action, "task": self._describe(manager, job)}
            if not any(
                key in kwargs
                for key in (
                    "name",
                    "note",
                    "background",
                    "session",
                    "run_at",
                    "cron_expression",
                    "run_once",
                    "timezone",
                )
            ):
                raise ValueError("edit 至少需要一个要修改的字段。")

        payload = dict(job.payload) if job else {}
        plugin_data = dict(task_metadata(payload))
        target = str(kwargs.get("session") or payload.get("session") or current).strip()
        if target != current:
            require_admin(event)
        resolve_session(self.context, target)
        tz_name = str(
            kwargs.get("timezone")
            or (job.timezone if job else None)
            or self.context.get_config(umo=target).get("timezone")
            or "UTC"
        ).strip()
        tz = ZoneInfo(tz_name)
        name = str(kwargs.get("name", job.name if job else "未来任务")).strip()
        note = str(
            kwargs.get(
                "note",
                plugin_data.get(
                    "instruction",
                    payload.get("note") or (job.description if job else ""),
                ),
            )
            or ""
        ).strip()
        background = str(
            kwargs.get(
                "background",
                plugin_data.get("background", plugin_data.get("context", "")),
            )
            or ""
        ).strip()
        if not name or not note:
            raise ValueError("name 和 note 不能为空；note 必须交代未来要完成的事。")
        run_at_text = kwargs.get("run_at", payload.get("run_at"))
        cron = str(
            kwargs.get("cron_expression", job.cron_expression if job else "") or ""
        ).strip()
        # 明确传入一种新日程时，清掉旧的另一种日程。
        if kwargs.get("run_at") and "cron_expression" not in kwargs:
            cron = ""
        if kwargs.get("cron_expression") and "run_at" not in kwargs:
            run_at_text = None
        once = bool(kwargs.get("run_once", bool(run_at_text)))
        if once:
            if not run_at_text or cron:
                raise ValueError("一次性任务只需 run_at，不要同时传 cron_expression。")
            run_at = datetime.fromisoformat(str(run_at_text))
            run_at = run_at.replace(tzinfo=tz) if run_at.tzinfo is None else run_at
            if run_at <= datetime.now(UTC):
                raise ValueError("run_at 必须晚于当前时间。")
            payload["run_at"] = run_at.isoformat()
            cron = None
        else:
            if not cron or run_at_text:
                raise ValueError(
                    "周期任务需要五段 cron_expression，不要同时传 run_at。"
                )
            run_at = None
            payload.pop("run_at", None)
        plugin_data.update(
            instruction=note,
            background=background,
            source_session=plugin_data.get("source_session") or current,
        )
        source_session = plugin_data["source_session"]
        payload.update(
            session=target,
            sender_id=payload.get("sender_id") or sender_id(event),
            origin=payload.get("origin", "tool"),
            krypton=plugin_data,
            note=note
            + (
                f"\n\n任务来源会话：{source_session}"
                if source_session != target
                else ""
            )
            + (f"\n\n背景与必要信息：\n{background}" if background else ""),
        )
        payload.pop("session_tasks", None)
        plugin_data.pop("context", None)
        candidate = CronJob(
            name=name,
            job_type="active_agent",
            cron_expression=cron,
            payload=payload,
            timezone=tz_name,
            run_once=once,
            enabled=job.enabled if job else True,
        )
        # 原生创建接口先落库再校验；先复用其校验器，防止无效日程留下脏记录。
        manager._build_trigger(candidate)
        if job:
            job = await manager.update_job(
                job.job_id,
                name=name,
                description=note,
                payload=payload,
                cron_expression=cron,
                run_once=once,
                timezone=tz_name,
            )
        else:
            job = await manager.add_active_job(
                name=name,
                description=note,
                payload=payload,
                cron_expression=cron,
                run_once=once,
                run_at=run_at,
                timezone=tz_name,
                persistent=True,
            )
        return {"action": action, "task": self._describe(manager, job)}
