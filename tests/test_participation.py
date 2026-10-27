import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot_plugin_krypton.main import KryptonPlugin
from astrbot_plugin_krypton.participation import ParticipationGate


@pytest.fixture
def group_case():
    meta = PlatformMetadata(name="aiocqhttp", description="QQ test", id="qq")
    platform = SimpleNamespace(meta=lambda: meta)
    context = SimpleNamespace(
        get_platform_inst=lambda _: platform,
        get_config=lambda umo=None: {"provider_settings": {"enable": True}},
        add_llm_tools=lambda *tools: None,
    )
    config = {
        "enable_jev": True,
        "typesafe_api_key": "test-only-key",
        "jev_group_sessions": ["qq:GroupMessage:123"],
    }
    judge = SimpleNamespace(score=AsyncMock(return_value=0.9), close=AsyncMock())

    def event(session="123", text="有人玩过这个吗"):
        message = AstrBotMessage()
        message.type = MessageType.GROUP_MESSAGE
        message.self_id = "bot"
        message.sender = MessageMember(user_id="member", nickname="群友")
        message.message = [Plain(text)]
        return AstrMessageEvent(text, message, meta, session)

    return context, config, judge, event


@pytest.mark.parametrize(
    "reason",
    [
        "disabled",
        "no_key",
        "no_scope",
        "native_off",
        "ai_off",
        "at",
        "command",
        "sent",
        "self",
    ],
)
async def test_normal_paths_never_call_jev(group_case, reason):
    context, config, judge, event = group_case
    msg = event()
    if reason == "disabled":
        config["enable_jev"] = False
    elif reason == "no_key":
        config["typesafe_api_key"] = ""
    elif reason == "no_scope":
        config["jev_group_sessions"] = []
    elif reason == "native_off":
        context.get_config = lambda umo=None: {
            "provider_settings": {"proactive_capability": {"add_cron_tools": False}}
        }
    elif reason == "ai_off":
        context.get_config = lambda umo=None: {"provider_settings": {"enable": False}}
    elif reason == "at":
        msg.is_at_or_wake_command = True
    elif reason == "command":
        msg.set_extra("handlers_parsed_params", {"command": {}})
    elif reason == "sent":
        msg._has_send_oper = True
    elif reason == "self":
        msg.message_obj.sender.user_id = "bot"
    original = msg.is_at_or_wake_command
    gate = ParticipationGate(context, config, judge)
    assert not await gate.consider(msg)
    assert msg.is_at_or_wake_command == original
    judge.score.assert_not_awaited()


@pytest.mark.parametrize("score, expected", [(0.9, True), (0.1, False)])
async def test_jev_result_uses_native_agent_pipeline_and_preserves_role(
    group_case, score, expected
):
    from astrbot.core.pipeline.process_stage.stage import ProcessStage

    context, config, judge, event = group_case
    judge.score.return_value = score
    plugin = KryptonPlugin(context, config)
    plugin.participation.judge = judge
    msg = event()
    msg.set_extra("activated_handlers", [])
    await plugin.judge_group_participation(msg)
    assert msg.is_at_or_wake_command == expected
    assert msg.role == "member"
    assert msg.message_str == "有人玩过这个吗"
    called = []

    async def process(event):
        called.append(event)
        yield None

    stage = ProcessStage()
    stage.ctx = SimpleNamespace(astrbot_config={"provider_settings": {"enable": True}})
    stage.agent_sub_stage = SimpleNamespace(process=process)
    async for _ in stage.process(msg):
        pass
    assert len(called) == int(expected)
    await plugin.terminate()


async def test_same_physical_group_serializes_checks_and_shares_cooldown(group_case):
    context, config, judge, event = group_case
    entered, release = asyncio.Event(), asyncio.Event()

    async def score(*args):
        entered.set()
        await release.wait()
        return 0.9

    judge.score.side_effect = score
    gate = ParticipationGate(context, config, judge)
    pending = asyncio.create_task(gate.consider(event("a_123")))
    await entered.wait()
    assert not await gate.consider(event("b_123"))
    release.set()
    assert await pending
    gate.last_check[("qq", "123")] = time.monotonic() - 1000
    assert not await gate.consider(event("c_123"))
    judge.score.assert_awaited_once()
    state = judge.score.call_args.args[1]
    assert len(state["recent_messages"]) == 1


async def test_judgment_failure_skips_and_cancellation_propagates(group_case):
    context, config, judge, event = group_case
    gate = ParticipationGate(context, config, judge)
    judge.score.side_effect = RuntimeError("test-only-error")
    assert not await gate.consider(event())
    gate.last_check.clear()
    judge.score.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await gate.consider(event())
    assert not gate.busy


@pytest.mark.parametrize(
    "action",
    [
        "disable",
        "reply",
        "unload",
        "key_removed",
        "scope_removed",
        "ai_off",
        "native_off",
    ],
)
async def test_pending_judgment_cannot_wake_after_state_changes(group_case, action):
    context, config, judge, event = group_case
    entered, release = asyncio.Event(), asyncio.Event()

    async def score(*args):
        entered.set()
        await release.wait()
        return 0.9

    judge.score.side_effect = score
    gate = ParticipationGate(context, config, judge)
    pending = asyncio.create_task(gate.consider(event()))
    await entered.wait()
    if action == "disable":
        config["enable_jev"] = False
    elif action == "unload":
        await gate.close()
    elif action == "key_removed":
        config["typesafe_api_key"] = ""
    elif action == "scope_removed":
        config["jev_group_sessions"] = []
    elif action == "ai_off":
        context.get_config = lambda umo=None: {"provider_settings": {"enable": False}}
    elif action == "native_off":
        context.get_config = lambda umo=None: {
            "provider_settings": {"proactive_capability": {"add_cron_tools": False}}
        }
    else:
        reply = event()
        reply.set_result(MessageEventResult().message("我来看看"))
        gate.note_sent(reply)
    release.set()
    assert not await pending


async def test_member_specific_scope_does_not_match_other_isolated_sessions(group_case):
    context, config, judge, event = group_case
    config["jev_group_sessions"] = ["qq:GroupMessage:a_123"]
    gate = ParticipationGate(context, config, judge)
    assert not await gate.consider(event("b_123"))
    assert await gate.consider(event("a_123"))
    judge.score.assert_awaited_once()


async def test_low_score_checks_are_paced_but_new_messages_still_enter_buffer(
    group_case,
):
    context, config, judge, event = group_case
    judge.score.return_value = 0.1
    gate = ParticipationGate(context, config, judge)
    assert not await gate.consider(event(text="第一条"))
    assert not await gate.consider(event(text="第二条"))
    judge.score.assert_awaited_once()
    gate.last_check[("qq", "123")] = time.monotonic() - 1000
    assert not await gate.consider(event(text="第三条"))
    assert judge.score.await_count == 2
    assert [m["text"] for m in judge.score.call_args.args[1]["recent_messages"]] == [
        "第一条",
        "第二条",
        "第三条",
    ]
