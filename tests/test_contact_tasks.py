import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api import ToolSet
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.tools.cron_tools import FutureTaskTool
from astrbot.core.tools.message_tools import SendMessageToUserTool
from astrbot_plugin_krypton.contact_delivery import ContactStopped
from astrbot_plugin_krypton.contact_tasks import contact_schedule
from astrbot_plugin_krypton.tasks import TaskService
from astrbot_plugin_krypton.tools import FutureTaskPlusTool, GetContactStateTool


def later(hours=1):
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


async def native_create(wrapper, **changes):
    args = dict(
        action="create",
        name="下次联系",
        note="看看有没有新的话题",
        run_once=True,
        run_at=later(),
    )
    args.update(changes)
    return await FutureTaskTool().call(wrapper, **args)


async def test_native_group_followup_keeps_owner_creator_and_is_editable(contact_case):
    c = contact_case
    session = MessageSession.from_str("custom-account:GroupMessage:999")
    event = CronMessageEvent(
        context=c.context,
        session=session,
        message="去群里找人",
        message_type=MessageType.GROUP_MESSAGE,
        extras={
            "cron_payload": {
                "sender_id": "123456",
                "krypton": {
                    "intent": "contact",
                    "contact_session": c.session,
                    "managed_private": True,
                },
            }
        },
    )
    event.role = "member"
    wrapper = await c.begin(event)
    assert (await native_create(wrapper)).startswith("Scheduled future task")
    (job,) = await c.context.cron_manager.list_jobs("active_agent")
    metadata = job.payload["krypton"]
    assert metadata["intent"] == "contact" and metadata["managed_private"]
    assert metadata["contact_session"] == c.session
    assert job.payload["sender_id"] == "123456"
    result = await FutureTaskTool().call(
        wrapper, action="edit", job_id=job.job_id, note="新的话题"
    )
    assert result.startswith("Updated future task")
    row = await c.context.get_db().get_cron_job(job.job_id)
    assert row.payload["krypton"]["instruction"] == "新的话题"
    assert event.role == "member"
    other = await c.context.cron_manager.add_active_job(
        name="其他成员的任务",
        cron_expression=None,
        run_once=True,
        run_at=datetime.now(UTC) + timedelta(hours=1),
        payload={"session": str(session), "sender_id": "654321", "note": "保持不变"},
    )
    denied = await FutureTaskTool().call(
        wrapper, action="edit", job_id=other.job_id, note="不应覆盖"
    )
    assert "created by another member" in denied
    assert (await c.context.get_db().get_cron_job(other.job_id)).payload[
        "note"
    ] == "保持不变"
    await c.runtime.close()


async def test_native_edit_changes_timing_and_preserves_business_identity(contact_case):
    c = contact_case
    event = c.cron()
    wrapper = await c.begin(event)
    await native_create(wrapper)
    (job,) = await c.context.cron_manager.list_jobs("active_agent")
    job.payload["krypton"]["timing_reason"] = "旧时间的依据"
    await c.context.cron_manager.update_job(job.job_id, payload=job.payload)
    result = await FutureTaskTool().call(
        wrapper, action="edit", job_id=job.job_id, run_at=later(2)
    )
    assert result.startswith("Updated")
    row = await c.context.get_db().get_cron_job(job.job_id)
    assert row.payload["krypton"]["timing_reason"] == ""
    service = TaskService(c.context, c.runtime)
    ordinary = await service.execute(
        event, "create", note="普通提醒", intent="task", run_at=later()
    )
    await FutureTaskTool().call(
        wrapper, action="edit", job_id=ordinary["task"]["job_id"], note="仍然是普通提醒"
    )
    row = await c.context.get_db().get_cron_job(ordinary["task"]["job_id"])
    assert row.payload["krypton"]["intent"] == "task"
    assert row.payload["krypton"]["instruction"] == "仍然是普通提醒"
    await c.runtime.close()


async def test_stale_turn_cannot_schedule_edit_or_resume_but_can_clean_up(contact_case):
    c = contact_case
    event = c.cron()
    wrapper = await c.begin(event)
    service = TaskService(c.context, c.runtime)
    created = await service.execute(event, "create", note="后续联系", run_at=later())
    job_id = created["task"]["job_id"]
    await service.execute(event, "pause", job_id=job_id)
    await c.runtime.observe(c.incoming())
    with pytest.raises(ContactStopped):
        await native_create(wrapper)
    with pytest.raises(ContactStopped):
        await FutureTaskTool().call(
            wrapper, action="edit", job_id=job_id, note="旧轮次不能修改"
        )
    tool = FutureTaskPlusTool(service=service)
    for action, args in [
        ("create", {"note": "不能创建", "run_at": later()}),
        ("edit", {"job_id": job_id, "note": "不能覆盖"}),
        ("resume", {"job_id": job_id}),
    ]:
        assert "已有新消息" in await tool.call(wrapper, action=action, **args)
    row = await c.context.get_db().get_cron_job(job_id)
    assert not row.enabled and row.payload["krypton"]["instruction"] == "后续联系"
    await service.execute(event, "pause", job_id=job_id)
    await service.execute(event, "delete", job_id=job_id)
    assert not await c.context.cron_manager.list_jobs("active_agent")
    facts = await c.runtime.state.facts(c.session)
    assert "已有新消息" in facts["last_result"]["reason"]
    await c.runtime.close()


async def test_native_invalid_schedule_does_not_leave_database_row(contact_case):
    c = contact_case
    wrapper = await c.begin(c.cron())
    result = await native_create(
        wrapper, run_once=False, run_at=None, cron_expression="invalid cron"
    )
    assert result.startswith("error:")
    assert not await c.context.cron_manager.list_jobs("active_agent")
    await c.runtime.close()


async def test_plain_native_task_does_not_acquire_contact_metadata(contact_case):
    c = contact_case
    wrapper = await c.begin(c.cron("task"))
    await native_create(wrapper)
    (job,) = await c.context.cron_manager.list_jobs("active_agent")
    assert "krypton" not in job.payload
    assert wrapper.context.context is c.context
    await c.runtime.close()


async def test_quiet_hours_block_sending_but_allow_planning_until_new_input(
    contact_case,
):
    c = contact_case
    event = c.cron()
    event.get_extra("cron_payload")["krypton"]["managed_private"] = True
    wrapper = await c.begin(event)
    c.runtime.private._outside_quiet = lambda session, now: now + 600
    service = TaskService(c.context, c.runtime)
    await service.execute(event, "create", note="免打扰后再联系", run_at=later())
    result = await SendMessageToUserTool().call(
        wrapper, messages=[{"type": "plain", "text": "现在不能发"}]
    )
    assert result.startswith("error:")
    await native_create(wrapper, name="安静等待后的日程")
    c.actual_send.assert_not_awaited()
    await c.runtime.observe(c.incoming())
    with pytest.raises(ContactStopped, match="已有新消息"):
        await native_create(wrapper, name="过期轮次不能继续")
    assert len(await c.context.cron_manager.list_jobs("active_agent")) == 2
    await c.runtime.close()


async def test_followup_summary_excludes_current_job_and_ordinary_reminders(
    contact_case,
):
    c = contact_case
    event = c.cron()
    await c.begin(event)
    service = TaskService(c.context, c.runtime)
    first = await service.execute(event, "create", note="当前任务", run_at=later())
    current = first["task"]["job_id"]
    event.set_extra("cron_job", {"id": current})
    await service.execute(
        event, "create", note="普通提醒", intent="task", run_at=later()
    )
    assert (await contact_schedule(c.context, c.session, current))["state"] == "none"
    paused = await service.execute(event, "create", note="暂停的联系", run_at=later())
    paused_id = paused["task"]["job_id"]
    await service.execute(event, "pause", job_id=paused_id)
    assert (await contact_schedule(c.context, c.session, current))["state"] == "paused"
    cross = await service.execute(
        event,
        "create",
        note="去群里联系",
        session="custom-account:GroupMessage:999",
        run_at=later(2),
        timing_reason="晚点去群里看看",
    )
    summary = await contact_schedule(c.context, c.session, current)
    assert summary["scheduled_count"] == 1 and summary["paused_count"] == 1
    assert summary["next_job_id"] == cross["task"]["job_id"]
    await c.runtime.finish(event)
    saved = (await c.runtime.state.facts(c.session))["last_result"]["followup"]
    assert saved == summary
    await c.runtime.close()


async def test_real_native_runner_can_continue_using_basic_task_tool(contact_case):
    c = contact_case
    event = c.cron()
    responses = [
        LLMResponse(
            role="assistant",
            completion_text="",
            tools_call_name=[name],
            tools_call_args=[args],
            tools_call_ids=[str(i)],
        )
        for i, (name, args) in enumerate(
            [
                (
                    "future_task",
                    {
                        "action": "create",
                        "name": "续聊",
                        "note": "下次聊",
                        "run_once": True,
                        "run_at": later(),
                    },
                ),
                ("get_contact_state", {}),
                (
                    "send_message_to_user",
                    {"messages": [{"type": "plain", "text": "先说一句。再说一句！"}]},
                ),
            ]
        )
    ] + [LLMResponse(role="assistant", completion_text="完成")]
    provider = SimpleNamespace(
        provider_config={"id": "native-contact", "max_context_tokens": 0},
        text_chat=AsyncMock(side_effect=responses),
    )

    class Hooks(BaseAgentRunHooks):
        async def on_agent_begin(self, context):
            await c.runtime.begin(event, context)

        async def on_tool_end(self, context, tool, args, result):
            c.runtime.reconcile_history(event)

        async def on_agent_done(self, context, response):
            await c.runtime.finish(event)

    runner = ToolLoopAgentRunner()
    await runner.reset(
        provider=provider,
        request=ProviderRequest(
            prompt="主动联系",
            func_tool=ToolSet(
                [
                    FutureTaskTool(),
                    GetContactStateTool(runtime=c.runtime),
                    SendMessageToUserTool(),
                ]
            ),
        ),
        run_context=ContextWrapper(
            context=SimpleNamespace(context=c.context, event=event)
        ),
        tool_executor=FunctionToolExecutor(),
        agent_hooks=Hooks(),
    )
    try:
        async for _ in runner.step_until_done(5):
            pass
        assert runner.state.name == "DONE"
        (job,) = await c.context.cron_manager.list_jobs("active_agent")
        assert job.payload["krypton"]["intent"] == "contact"
        c.actual_send.assert_awaited()
        assert c.actual_send.await_count == 2
        facts = await c.runtime.state.facts(c.session)
        assert facts["unanswered_contacts"] == 1
        assert facts["last_result"]["followup"]["scheduled_count"] == 1
        wrapper = ContextWrapper(
            context=SimpleNamespace(context=c.context, event=event)
        )
        result = json.loads(await GetContactStateTool(runtime=c.runtime).call(wrapper))
        assert result["followup"]["state"] == "scheduled"
        assert result["followup"]["next_run_time"].endswith("+00:00")
    finally:
        await c.runtime.close()
