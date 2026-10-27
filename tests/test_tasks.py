import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.cron.manager import CronJobManager, CronJobSchedulingError
from astrbot.core.platform.message_session import MessageSession
from astrbot_plugin_krypton.sessions import list_sessions
from astrbot_plugin_krypton.tasks import TaskService
from astrbot_plugin_krypton.tools import FutureTaskPlusTool


def future_time():
    return (datetime.now(UTC) + timedelta(days=365)).isoformat()


async def test_cross_session_lifecycle_with_real_database_scheduler_and_restart(
    context, event
):
    await context.get_db().upsert_umo_alias(
        "test:GroupMessage:target", "owner", "目标群", None
    )
    target = (await list_sessions(context, event, search="目标群"))["sessions"][0][
        "session"
    ]
    service = TaskService(context)
    created = await service.execute(
        event,
        "create",
        session=target,
        name="日报",
        note="查天气后发日报",
        background="城市：上海；只报告当天数据",
        cron_expression="0 8 * * mon-fri",
    )
    job_id = created["task"]["job_id"]
    assert created["task"]["next_run_time"].endswith("+08:00")
    assert created["task"]["source_session"] == event.unified_msg_origin
    row = await context.get_db().get_cron_job(job_id)
    assert row.payload["session"] == target
    assert row.payload["sender_id"] == "owner"
    assert "城市：上海" in row.payload["note"]
    assert (
        row.payload["note"]
        == "查天气后发日报\n\n任务来源会话：test:GroupMessage:home\n\n背景与必要信息：\n城市：上海；只报告当天数据"
    )
    assert (await service.execute(event, "list"))["total"] == 1
    assert (await service.execute(event, "get", job_id=job_id))["task"][
        "note"
    ] == "查天气后发日报"
    paused = await service.execute(event, "pause", job_id=job_id)
    assert paused["task"]["status"] == "paused"
    assert paused["task"]["next_run_time"] is None
    assert context.cron_manager.scheduler.get_job(job_id) is None
    await service.execute(event, "resume", job_id=job_id)
    assert context.cron_manager.scheduler.get_job(job_id) is not None

    await context.cron_manager.shutdown()
    restarted = CronJobManager(context.get_db())
    context.cron_manager = restarted
    await restarted.start(context)
    try:
        assert restarted.scheduler.get_job(job_id) is not None
        assert (await service.execute(event, "list", scope="all"))["total"] == 1
        await service.execute(event, "delete", job_id=job_id)
        assert await context.get_db().get_cron_job(job_id) is None
    finally:
        await restarted.shutdown()


async def test_one_shot_wakeup_retains_group_creator_and_can_schedule_followup(
    context, event
):
    event.role = "member"
    service = TaskService(context)
    created = await service.execute(
        event, "create", note="检查结果", run_at=future_time()
    )
    job_id = created["task"]["job_id"]
    wake = AsyncMock()
    context.cron_manager._woke_main_agent = wake
    await context.cron_manager._run_job(job_id)
    assert await context.get_db().get_cron_job(job_id) is None
    triggered = wake.call_args.kwargs
    assert triggered["session_str"] == event.unified_msg_origin
    cron_event = CronMessageEvent(
        context=context,
        session=MessageSession.from_str(triggered["session_str"]),
        message=triggered["message"],
        extras=triggered["extras"],
        message_type=MessageSession.from_str(triggered["session_str"]).message_type,
    )
    # 原生事件此时使用群 ID；插件须从原生任务载荷恢复真实创建者。
    assert cron_event.get_sender_id() != "owner"
    wrapper = SimpleNamespace(context=SimpleNamespace(event=cron_event))
    tool = FutureTaskPlusTool(service=service)
    result = json.loads(
        await tool.call(
            wrapper, action="create", note="稍后继续检查", run_at=future_time()
        )
    )
    assert "error" not in result
    next_id = result["task"]["job_id"]
    assert (await context.get_db().get_cron_job(next_id)).payload[
        "sender_id"
    ] == "owner"
    assert (await service.execute(event, "list"))["total"] == 1
    await service.execute(event, "delete", job_id=next_id)


async def test_edit_switches_schedules_and_preserves_payload(context, event):
    service = TaskService(context)
    created = await service.execute(
        event, "create", note="任务", cron_expression="0 8 * * *"
    )
    job_id = created["task"]["job_id"]
    row = await context.get_db().get_cron_job(job_id)
    row.payload["extension_data"] = {"keep": True}
    await context.get_db().update_cron_job(job_id, payload=row.payload)
    one_shot = await service.execute(event, "edit", job_id=job_id, run_at=future_time())
    assert one_shot["task"]["run_once"] is True
    assert one_shot["task"]["cron_expression"] is None
    recurring = await service.execute(
        event, "edit", job_id=job_id, cron_expression="0 9 * * mon"
    )
    assert recurring["task"]["run_once"] is False
    assert recurring["task"]["run_at"] is None
    assert (await context.get_db().get_cron_job(job_id)).payload["extension_data"] == {
        "keep": True
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {"cron_expression": "bad"},
        {"run_at": "2020-01-01T00:00:00+00:00"},
        {"run_at": "bad"},
        {"run_at": "2099-01-01T00:00:00Z", "cron_expression": "0 8 * * *"},
        {"cron_expression": "0 8 * * *", "timezone": "Invalid/Zone"},
        {"cron_expression": "0 8 * * *", "session": "unknown:GroupMessage:target"},
    ],
)
async def test_invalid_create_does_not_persist(context, event, arguments):
    tool = FutureTaskPlusTool(service=TaskService(context))
    wrapper = SimpleNamespace(context=SimpleNamespace(event=event))
    result = json.loads(
        await tool.call(wrapper, action="create", note="任务", **arguments)
    )
    assert "error" in result
    assert await context.cron_manager.list_jobs() == []


async def test_invalid_edit_does_not_replace_existing_schedule(context, event):
    service = TaskService(context)
    job_id = (
        await service.execute(
            event, "create", note="原任务", cron_expression="0 8 * * *"
        )
    )["task"]["job_id"]
    scheduled = context.cron_manager.scheduler.get_job(job_id)
    with pytest.raises(CronJobSchedulingError):
        await service.execute(
            event, "edit", job_id=job_id, note="不应写入", cron_expression="bad"
        )
    assert context.cron_manager.scheduler.get_job(job_id) is scheduled
    row = await context.get_db().get_cron_job(job_id)
    assert row.description == "原任务"
    assert row.cron_expression == "0 8 * * *"


@pytest.mark.parametrize("action", ["get", "edit", "pause", "resume", "delete"])
async def test_members_cannot_manage_other_creators(context, event, action):
    service = TaskService(context)
    job_id = (
        await service.execute(
            event, "create", note="原任务", cron_expression="0 8 * * *"
        )
    )["task"]["job_id"]
    event.role = "member"
    event.get_sender_id = lambda: "other-user"
    with pytest.raises(PermissionError):
        await service.execute(event, action, job_id=job_id, note="覆盖")
    assert (await service.execute(event, "list"))["total"] == 0
    row = await context.get_db().get_cron_job(job_id)
    assert row.enabled is True and row.description == "原任务"


async def test_member_cross_session_and_global_listing_are_rejected(context, event):
    event.role = "member"
    service = TaskService(context)
    with pytest.raises(PermissionError):
        await service.execute(
            event,
            "create",
            note="越权",
            session="test:GroupMessage:other",
            run_at=future_time(),
        )
    with pytest.raises(PermissionError):
        await service.execute(event, "list", scope="all")
    assert await context.cron_manager.list_jobs() == []


async def test_administrator_can_manage_dashboard_tasks_but_not_basic_jobs(
    context, event
):
    manager = context.cron_manager
    dashboard = await manager.add_active_job(
        name="WebUI任务",
        cron_expression="0 8 * * *",
        payload={"origin": "api", "session": event.unified_msg_origin, "note": "任务"},
    )
    basic = await context.get_db().create_cron_job(
        name="插件内部维护", job_type="basic", cron_expression="0 8 * * *"
    )
    service = TaskService(context)
    assert (await service.execute(event, "get", job_id=dashboard.job_id))["task"][
        "note"
    ] == "任务"
    assert (await service.execute(event, "list", scope="all"))["total"] == 1
    with pytest.raises(PermissionError):
        await service.execute(event, "delete", job_id=basic.job_id)


async def test_legacy_invalid_timezone_does_not_break_task_listing(context, event):
    await context.get_db().create_cron_job(
        name="旧任务",
        job_type="active_agent",
        cron_expression="0 8 * * *",
        timezone="Invalid/Zone",
        enabled=False,
        payload={"session": event.unified_msg_origin, "note": "原生记录"},
    )
    result = await TaskService(context).execute(event, "list")
    assert result["total"] == 1
    assert result["tasks"][0]["timezone"] == "UTC"
    assert result["tasks"][0]["configured_timezone"] == "Invalid/Zone"


async def test_renamed_plugin_preserves_legacy_task_background(context, event):
    row = await context.cron_manager.add_active_job(
        name="旧版任务",
        cron_expression="0 8 * * *",
        payload={
            "session": event.unified_msg_origin,
            "sender_id": "owner",
            "note": "旧指令",
            "session_tasks": {
                "instruction": "旧指令",
                "context": "必须保留的背景",
                "source_session": event.unified_msg_origin,
            },
        },
    )
    service = TaskService(context)
    old = await service.execute(event, "get", job_id=row.job_id)
    assert old["task"]["background"] == "必须保留的背景"
    await service.execute(event, "edit", job_id=row.job_id, note="更新后的指令")
    updated = await context.get_db().get_cron_job(row.job_id)
    assert updated.payload["krypton"]["background"] == "必须保留的背景"
    assert "session_tasks" not in updated.payload
    assert "context" not in updated.payload["krypton"]


@pytest.mark.parametrize(
    "action", ["create", "list", "get", "edit", "pause", "resume", "delete"]
)
async def test_native_switch_blocks_all_tool_actions_without_touching_jobs(
    context, event, action
):
    context.get_config = lambda umo=None: {
        "provider_settings": {"proactive_capability": {"add_cron_tools": False}}
    }
    tool = FutureTaskPlusTool(service=TaskService(context))
    wrapper = SimpleNamespace(context=SimpleNamespace(context=context, event=event))
    result = await tool.call(
        wrapper, action=action, note="不应建立的任务", job_id="not-present"
    )
    assert "原生主动型能力已关闭" in result
    assert await context.cron_manager.list_jobs() == []
