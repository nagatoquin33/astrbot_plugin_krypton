"""为主动轮次内的原生任务调用保留联系身份，并查询后续日程。"""

import copy
from datetime import UTC, datetime

from astrbot.core.db.po import CronJob

from .sessions import sender_id
from .tasks import task_metadata


class ContactTaskManager:
    """仅代理当前主动 Agent 的调度入口，不改动共享调度器。"""

    def __init__(self, original, runtime, event):
        self.original, self.runtime, self.event = original, runtime, event

    def __getattr__(self, name):
        return getattr(self.original, name)

    async def _guard(self):
        await self.runtime.guard(
            self.event.get_extra("krypton_contact_turn"), for_task=True
        )

    async def add_active_job(self, **kwargs):
        await self._guard()
        payload = copy.deepcopy(kwargs["payload"])
        parent = task_metadata(self.event.get_extra("cron_payload", {}) or {})
        metadata = dict(task_metadata(payload))
        if "intent" not in metadata:
            metadata.update(
                intent="contact",
                contact_session=parent.get("contact_session")
                or self.event.unified_msg_origin,
                managed_private=bool(
                    parent.get("managed_private") or parent.get("private_bootstrap")
                ),
                source_session=self.event.unified_msg_origin,
                instruction=payload.get("note", ""),
                timing_reason="",
                decided_at=datetime.now(UTC).isoformat(),
            )
            payload["krypton"] = metadata
            payload["sender_id"] = sender_id(self.event)
        # 基础工具在落库后才校验日程；先检查，避免错误参数留下无效任务。
        if kwargs.get("run_once") and kwargs.get("run_at"):
            payload["run_at"] = kwargs["run_at"].isoformat()
        self.original._build_trigger(
            CronJob(
                name=kwargs["name"],
                job_type="active_agent",
                payload=payload,
                cron_expression=kwargs.get("cron_expression"),
                timezone=kwargs.get("timezone"),
                run_once=kwargs.get("run_once", False),
            )
        )
        await self._guard()
        return await self.original.add_active_job(**{**kwargs, "payload": payload})

    async def update_job(self, job_id, **kwargs):
        if kwargs.get("enabled") is not False:
            await self._guard()
        job = await self.original.db.get_cron_job(job_id)
        if job and task_metadata(job.payload):
            payload = copy.deepcopy(kwargs.get("payload", job.payload))
            metadata = dict(task_metadata(payload))
            if "description" in kwargs:
                metadata["instruction"] = kwargs["description"]
            if (
                payload.get("run_at") != job.payload.get("run_at")
                or kwargs.get("cron_expression", job.cron_expression)
                != job.cron_expression
                or kwargs.get("timezone", job.timezone) != job.timezone
            ):
                metadata["timing_reason"] = ""
                metadata["decided_at"] = datetime.now(UTC).isoformat()
            payload["krypton"] = metadata
            kwargs["payload"] = payload
        if kwargs.get("enabled") is not False:
            await self._guard()
        return await self.original.update_job(job_id, **kwargs)


async def contact_schedule(context, owner, exclude_job_id=""):
    """只读当前联系对象的日程，排除正在结束的这一项任务。"""
    result = dict(
        state="none",
        scheduled_count=0,
        running_count=0,
        paused_count=0,
        unscheduled_count=0,
    )
    manager = context.cron_manager
    if manager is None:
        result["state"] = "unavailable"
        return result
    next_job, next_time = None, None
    for job in await manager.list_jobs("active_agent"):
        metadata = task_metadata(job.payload)
        if (
            job.job_id == exclude_job_id
            or metadata.get("intent") != "contact"
            or (metadata.get("contact_session") or job.payload.get("session")) != owner
        ):
            continue
        if job.status == "running":
            result["running_count"] += 1
        elif not job.enabled:
            result["paused_count"] += 1
        elif (when := manager.get_next_run_time(job.job_id)) is not None:
            result["scheduled_count"] += 1
            if next_time is None or when < next_time:
                next_job, next_time = job, when
        else:
            result["unscheduled_count"] += 1
    if next_job:
        result.update(
            state="scheduled",
            next_job_id=next_job.job_id,
            next_name=next_job.name,
            next_run_time=next_time.astimezone(UTC).isoformat(),
            timing_reason=task_metadata(next_job.payload).get("timing_reason", ""),
        )
    else:
        for name in ("running", "paused", "unscheduled"):
            if result[name + "_count"]:
                result["state"] = name
                break
    return result
