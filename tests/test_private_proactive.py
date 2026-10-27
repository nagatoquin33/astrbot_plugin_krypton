import asyncio
import copy
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot_plugin_krypton.private_proactive import (
    OWNER_KEY,
    STATE_KEY,
    PrivateProactive,
)
from astrbot_plugin_krypton.tasks import TaskService


@pytest.fixture
def private_case(context, event, onebot):
    config = {
        "enable_private_proactive": True,
        "private_proactive_idle_minutes": 30,
        "private_proactive_qq_ids": ["123456"],
    }
    native = {"timezone": "Asia/Shanghai", "admins_id": ["123456"]}
    context.get_config = lambda umo=None: native
    context.cron_manager._woke_main_agent = AsyncMock()
    event.unified_msg_origin = "custom-account:FriendMessage:123456"
    event.get_message_type = lambda: MessageType.FRIEND_MESSAGE
    event.get_self_id = lambda: "bot"
    event.get_sender_id = lambda: "123456"
    store = {}
    clock = [time.time()]

    async def load(key, default):
        return copy.deepcopy(store.get(key, default))

    async def save(key, value):
        store[key] = copy.deepcopy(value)

    def service():
        return PrivateProactive(context, config, load, save, clock=lambda: clock[0])

    return SimpleNamespace(
        context=context,
        event=event,
        config=config,
        native=native,
        store=store,
        clock=clock,
        service=service,
    )


async def test_ordinary_private_chat_automatically_wakes_native_agent_without_jev(
    private_case,
):
    case = private_case
    service = case.service()
    await service.on_message(case.event)
    session = case.event.unified_msg_origin
    job_id = service.jobs[session]
    job = await case.context.cron_manager.db.get_cron_job(job_id)
    assert job.run_once and not job.persistent
    assert job.payload[OWNER_KEY] is True
    assert case.store[STATE_KEY][session]["deadline"] == case.clock[0] + 1800
    await case.context.cron_manager._run_job(job_id)
    wake = case.context.cron_manager._woke_main_agent
    wake.assert_awaited_once()
    payload = wake.call_args.kwargs["extras"]["cron_payload"]
    assert payload["session"] == session
    assert payload["sender_id"] == "123456" and payload["origin"] == "tool"
    assert "自己决定" in payload["note"] and "future_task_plus" in payload["note"]
    assert case.store[STATE_KEY][session]["consumed"]
    assert await case.context.cron_manager.db.get_cron_job(job_id) is None
    case.context.llm_generate.assert_not_awaited()


async def test_native_scheduler_triggers_private_wakeup_without_manual_execution(
    private_case,
    monkeypatch,
):
    case = private_case
    service = case.service()
    finished = asyncio.Event()
    manager = case.context.cron_manager
    original = manager._run_job

    async def execute(*args, **kwargs):
        try:
            await original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(manager, "_run_job", execute)
    await service.on_message(case.event)
    session = case.event.unified_msg_origin
    state = service.states[session]
    state["deadline"] = time.time() + 0.3
    await service._schedule(session, state)
    job_id = service.jobs[session]

    await asyncio.wait_for(finished.wait(), timeout=5)
    assert await manager.db.get_cron_job(job_id) is None
    case.context.cron_manager._woke_main_agent.assert_awaited_once()
    assert case.store[STATE_KEY][session]["consumed"]


async def test_new_message_and_normal_reply_reset_single_pending_wakeup(private_case):
    case = private_case
    service = case.service()
    session = case.event.unified_msg_origin
    await service.on_message(case.event)
    old_job = service.jobs[session]
    old_token = service.states[session]["token"]
    case.clock[0] += 10
    await service.on_reply(case.event)
    assert service.states[session]["deadline"] == case.clock[0] + 1800
    assert await case.context.cron_manager.db.get_cron_job(old_job) is None
    new_job = service.jobs[session]
    await service._run(session=session, token=old_token)
    assert service.jobs[session] == new_job
    case.context.cron_manager._woke_main_agent.assert_not_awaited()
    assert len(await case.context.cron_manager.list_jobs("basic")) == 1


async def test_start_uses_only_configured_qq_even_without_prior_conversation(
    private_case,
):
    case = private_case
    await case.context.get_db().upsert_umo_alias(
        "custom-account:FriendMessage:654321", "654321", "其他管理员", None
    )
    case.native["admins_id"].append("654321")
    service = case.service()
    await service.start()
    assert list(service.jobs) == [case.event.unified_msg_origin]
    await service.start()
    assert len(await case.context.cron_manager.list_jobs("basic")) == 1


async def test_empty_qq_list_does_not_discover_administrator_conversations(
    private_case,
):
    case = private_case
    case.config["private_proactive_qq_ids"] = []
    await case.context.get_db().upsert_umo_alias(
        case.event.unified_msg_origin, "123456", "已有管理员私聊", None
    )
    service = case.service()
    await service.start()
    assert not service.states and not service.jobs
    assert not await case.context.cron_manager.list_jobs("basic")


async def test_pending_state_restores_and_missed_deadline_does_not_burst_on_restart(
    private_case,
):
    case = private_case
    service = case.service()
    await service.on_message(case.event)
    session = case.event.unified_msg_origin
    old_job = service.jobs[session]
    await service.close()
    case.clock[0] += 3600
    restored = case.service()
    await restored.start()
    assert restored.jobs[session] != old_job
    assert restored.states[session]["deadline"] == case.clock[0] + 1800
    assert len(await case.context.cron_manager.list_jobs("basic")) == 1


async def test_failed_agent_wakeup_is_consumed_and_never_replayed(private_case):
    case = private_case
    service = case.service()
    await service.on_message(case.event)
    case.context.cron_manager._woke_main_agent.side_effect = RuntimeError(
        "model unavailable"
    )
    await case.context.cron_manager._run_job(
        service.jobs[case.event.unified_msg_origin]
    )
    await service.close()
    restored = case.service()
    await restored.start()
    assert not restored.jobs
    case.context.cron_manager._woke_main_agent.assert_awaited_once()


@pytest.mark.parametrize(
    "blocked",
    [
        "disabled",
        "no_targets",
        "not_listed",
        "native_off",
        "ai_off",
        "plugin_off",
        "group",
    ],
)
async def test_private_startup_respects_enablement_and_scope(private_case, blocked):
    case = private_case
    if blocked == "disabled":
        case.config["enable_private_proactive"] = False
    elif blocked == "no_targets":
        case.config["private_proactive_qq_ids"] = []
    elif blocked == "not_listed":
        case.config["private_proactive_qq_ids"] = ["654321"]
    elif blocked == "native_off":
        case.native["provider_settings"] = {
            "proactive_capability": {"add_cron_tools": False}
        }
    elif blocked == "ai_off":
        case.native["provider_settings"] = {"enable": False}
    elif blocked == "plugin_off":
        case.native["plugin_set"] = ["another-plugin"]
    else:
        case.event.get_message_type = lambda: MessageType.GROUP_MESSAGE
    service = case.service()
    await service.on_message(case.event)
    assert not service.jobs
    assert not await case.context.cron_manager.list_jobs()


async def test_configured_member_session_uses_real_sender_without_admin_escalation(
    private_case,
):
    case = private_case
    case.native["admins_id"] = []
    service = case.service()
    await service.on_message(case.event)
    await case.context.cron_manager._run_job(
        service.jobs[case.event.unified_msg_origin]
    )
    payload = case.context.cron_manager._woke_main_agent.call_args.kwargs["extras"][
        "cron_payload"
    ]
    assert payload["sender_id"] == "123456" and payload["origin"] == "tool"


async def test_disable_before_timer_fires_prevents_model_call(private_case):
    case = private_case
    service = case.service()
    await service.on_message(case.event)
    case.config["enable_private_proactive"] = False
    await case.context.cron_manager._run_job(
        service.jobs[case.event.unified_msg_origin]
    )
    case.context.cron_manager._woke_main_agent.assert_not_awaited()


async def test_quiet_hours_apply_at_creation_and_are_rechecked_before_wakeup(
    private_case,
):
    case = private_case
    case.clock[0] = datetime(2027, 1, 1, 14, 40, tzinfo=UTC).timestamp()
    case.config["private_proactive_quiet_hours"] = "23:00-08:00"
    service = case.service()
    await service.on_message(case.event)
    session = case.event.unified_msg_origin
    assert (
        service.states[session]["deadline"]
        == datetime(2027, 1, 2, 0, tzinfo=UTC).timestamp()
    )
    case.clock[0] = datetime(2027, 1, 1, 16, tzinfo=UTC).timestamp()
    old_job = service.jobs[session]
    await case.context.cron_manager._run_job(old_job)
    assert service.jobs[session] != old_job
    assert not service.states[session]["consumed"]
    case.context.cron_manager._woke_main_agent.assert_not_awaited()


async def test_native_agent_can_schedule_its_own_followup_and_plugin_leaves_it_intact(
    private_case,
):
    case = private_case
    service = case.service()

    async def wake(**kwargs):
        event = CronMessageEvent(
            context=case.context,
            session=MessageSession.from_str(kwargs["session_str"]),
            message=kwargs["message"],
            extras=kwargs["extras"],
            message_type=MessageType.FRIEND_MESSAGE,
        )
        event.role = "admin"
        await TaskService(case.context).execute(
            event,
            "create",
            name="自己安排的后续联系",
            note="看完资料后分享",
            run_at=(datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        )

    case.context.cron_manager._woke_main_agent.side_effect = wake
    await service.on_message(case.event)
    await case.context.cron_manager._run_job(
        service.jobs[case.event.unified_msg_origin]
    )
    await service.close()
    followups = await case.context.cron_manager.list_jobs("active_agent")
    assert len(followups) == 1
    assert followups[0].payload["sender_id"] == "123456"
    assert followups[0].payload["session"] == case.event.unified_msg_origin


async def test_new_private_message_cancels_an_unfinished_automatic_wakeup(private_case):
    case = private_case
    service = case.service()
    entered = asyncio.Event()

    async def wait(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    case.context.cron_manager._woke_main_agent.side_effect = wait
    await service.on_message(case.event)
    job = service.jobs[case.event.unified_msg_origin]
    pending = asyncio.create_task(case.context.cron_manager._run_job(job))
    await entered.wait()
    await service.on_message(case.event)
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not service.running
    assert service.jobs[case.event.unified_msg_origin] != job


async def create_future(case, *, session=None, source=None, enabled=True):
    payload = {
        "session": session or case.event.unified_msg_origin,
        "sender_id": "123456",
        "note": "已有后续日程",
    }
    if source:
        payload["krypton"] = {"source_session": source}
    return await case.context.cron_manager.add_active_job(
        name="已有未来任务",
        cron_expression=None,
        run_once=True,
        run_at=datetime.now(UTC) + timedelta(hours=2),
        payload=payload,
        enabled=enabled,
    )


@pytest.mark.parametrize("kind", ["pending", "running", "source"])
async def test_existing_related_future_task_prevents_startup_wakeup(private_case, kind):
    case = private_case
    session = case.event.unified_msg_origin
    job = await create_future(
        case,
        session="custom-account:GroupMessage:999" if kind == "source" else session,
        source=session if kind == "source" else None,
        enabled=kind != "running",
    )
    if kind == "running":
        await case.context.cron_manager.db.update_cron_job(job.job_id, status="running")
    service = case.service()
    await service.start()
    assert not service.jobs
    assert service.states[session]["consumed"]
    assert await case.context.cron_manager.db.get_cron_job(job.job_id) is not None


@pytest.mark.parametrize("paused", [False, True])
async def test_unrelated_or_paused_tasks_do_not_block_target_bootstrap(
    private_case, paused
):
    case = private_case
    await create_future(
        case,
        session=case.event.unified_msg_origin
        if paused
        else "custom-account:FriendMessage:654321",
        enabled=not paused,
    )
    service = case.service()
    await service.start()
    assert case.event.unified_msg_origin in service.jobs


async def test_timer_rechecks_new_future_task_before_waking_agent(private_case):
    case = private_case
    service = case.service()
    await service.on_message(case.event)
    timer = service.jobs[case.event.unified_msg_origin]
    future = await create_future(case)
    await case.context.cron_manager._run_job(timer)
    case.context.cron_manager._woke_main_agent.assert_not_awaited()
    assert not service.jobs
    assert await case.context.cron_manager.db.get_cron_job(future.job_id) is not None


async def test_reply_after_scheduling_future_cancels_pending_bootstrap(private_case):
    case = private_case
    service = case.service()
    await service.on_message(case.event)
    timer = service.jobs[case.event.unified_msg_origin]
    await create_future(case)
    await service.on_reply(case.event)
    assert not service.jobs
    assert await case.context.cron_manager.db.get_cron_job(timer) is None


async def test_multiple_onebot_accounts_require_explicit_selection(private_case):
    from astrbot.core.platform.platform_metadata import PlatformMetadata

    case = private_case
    meta = PlatformMetadata(
        name="aiocqhttp", description="第二账号", id="second-account"
    )
    case.context.platform_manager.platform_insts.append(
        SimpleNamespace(meta=lambda: meta)
    )
    service = case.service()
    await service.start()
    assert not service.jobs
    case.config["private_proactive_platform_id"] = "second-account"
    selected = case.service()
    await selected.start()
    assert list(selected.jobs) == ["second-account:FriendMessage:123456"]
