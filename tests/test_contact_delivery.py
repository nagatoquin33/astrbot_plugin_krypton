import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import At, Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.star_handler import EventType
from astrbot.core.tools.message_tools import SendMessageToUserTool
from astrbot_plugin_krypton.contact_delivery import ContactStopped
from astrbot_plugin_krypton.contact_segments import build_parts, split_text
from astrbot_plugin_krypton.tools import GetContactStateTool


async def test_native_send_tool_segments_text_and_media_and_counts_one_contact(
    contact_case,
):
    c = contact_case
    event = c.cron()
    wrapper = await c.begin(event)
    result = await SendMessageToUserTool().call(
        wrapper,
        messages=[
            {"type": "mention_user", "mention_user_id": "123456"},
            {"type": "plain", "text": "第一句。第二句！"},
            {"type": "image", "url": "https://example.test/picture.png"},
            {"type": "plain", "text": "尾巴"},
        ],
    )
    assert result == f"Message sent to session {c.session}"
    chains = [call.args[1].chain for call in c.actual_send.call_args_list]
    assert len(chains) == 4
    assert isinstance(chains[0][0], At)
    assert [
        comp.text for chain in chains for comp in chain if isinstance(comp, Plain)
    ] == ["第一句。", "第二句！", "尾巴"]
    assert isinstance(chains[2][0], Image)
    await SendMessageToUserTool().call(
        wrapper, messages=[{"type": "plain", "text": "补充一句"}]
    )
    await c.runtime.finish(event)
    facts = await c.runtime.state.facts(c.session)
    assert facts["unanswered_contacts"] == 1
    assert facts["last_result"]["sent_parts"] == 5
    await c.runtime.close()


async def test_business_task_and_shared_original_tool_keep_their_original_behavior(
    contact_case,
):
    c = contact_case
    event = c.cron("task")
    wrapper = await c.begin(event)
    assert c.context.send_message is c.actual_send
    await SendMessageToUserTool().call(
        wrapper, messages=[{"type": "plain", "text": "第一句。第二句！"}]
    )
    c.actual_send.assert_awaited_once()
    assert not event.get_extra("krypton_contact_turn")
    await c.begin(c.cron())
    c.actual_send.reset_mock()
    business = await c.begin(c.cron("task"))
    await SendMessageToUserTool().call(
        business, messages=[{"type": "plain", "text": "普通任务仍整条。不要拦截！"}]
    )
    c.actual_send.assert_awaited_once()
    await c.runtime.close()


async def test_output_plugin_can_send_early_parts_and_return_last_part_without_double_split(
    contact_case,
):
    c = contact_case

    async def hook(event, kind):
        if kind == EventType.OnDecoratingResultEvent:
            await c.context.send_message(
                event.unified_msg_origin, MessageChain([Plain("前一段")])
            )
            event.get_result().chain = [Plain("最后一段。保留一起！")]

    c.runtime.hook = hook
    event = c.cron()
    wrapper = await c.begin(event)
    await SendMessageToUserTool().call(
        wrapper, messages=[{"type": "plain", "text": "原始正文。"}]
    )
    await c.runtime.finish(event)
    texts = [call.args[1].get_plain_text() for call in c.actual_send.call_args_list]
    assert texts == ["前一段", "最后一段。保留一起！"]
    assert event.get_extra("_send_message_to_user_current_session_plain_texts") == texts
    assert (await c.runtime.state.facts(c.session))["unanswered_contacts"] == 1
    await c.runtime.close()


async def test_new_user_message_between_parts_stops_remaining_parts(contact_case):
    c = contact_case

    async def pause(*args):
        await c.runtime.observe(c.incoming())

    c.runtime.sleep = pause
    event = c.cron()
    wrapper = await c.begin(event)
    result = await SendMessageToUserTool().call(
        wrapper, messages=[{"type": "plain", "text": "第一句。第二句！"}]
    )
    assert result.startswith("error:")
    c.actual_send.assert_awaited_once()
    facts = await c.runtime.state.facts(c.session)
    assert facts["unanswered_contacts"] == 0
    assert facts["last_result"]["status"] == "partial"
    await c.runtime.close()


@pytest.mark.parametrize("second", [False, RuntimeError("unknown transport outcome")])
async def test_partial_or_unknown_delivery_never_replays_confirmed_parts(
    contact_case, second
):
    c = contact_case
    c.actual_send.side_effect = [True, second]
    event = c.cron()
    wrapper = await c.begin(event)
    messages = [{"type": "plain", "text": "第一句。第二句！"}]
    assert (await SendMessageToUserTool().call(wrapper, messages=messages)).startswith(
        "error:"
    )
    assert (await SendMessageToUserTool().call(wrapper, messages=messages)).startswith(
        "error:"
    )
    assert c.actual_send.await_count == 2
    facts = await c.runtime.state.facts(c.session)
    assert facts["unanswered_contacts"] == 1
    assert facts["last_result"]["status"] == (
        "unknown" if isinstance(second, Exception) else "partial"
    )
    await c.runtime.close()


async def test_successful_identical_tool_retry_is_not_sent_twice(contact_case):
    c = contact_case
    wrapper = await c.begin(c.cron())
    for _ in range(2):
        await SendMessageToUserTool().call(
            wrapper, messages=[{"type": "plain", "text": "只发一次"}]
        )
    c.actual_send.assert_awaited_once()
    await c.runtime.close()


async def test_bookkeeping_failure_after_confirmed_send_does_not_resend(contact_case):
    c = contact_case
    wrapper = await c.begin(c.cron())

    async def unavailable(*args):
        raise RuntimeError("state store unavailable")

    c.runtime.state.save = unavailable
    messages = [{"type": "plain", "text": "已确认送出"}]
    assert (await SendMessageToUserTool().call(wrapper, messages=messages)).startswith(
        "error:"
    )
    assert (await SendMessageToUserTool().call(wrapper, messages=messages)).startswith(
        "error:"
    )
    c.actual_send.assert_awaited_once()
    await c.runtime.close()


async def test_regular_send_in_another_task_is_not_routed_into_contact(contact_case):
    c = contact_case
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(event, kind):
        if kind == EventType.OnDecoratingResultEvent:
            entered.set()
            await release.wait()

    c.runtime.hook = hook
    wrapper = await c.begin(c.cron())
    pending = asyncio.create_task(
        SendMessageToUserTool().call(
            wrapper, messages=[{"type": "plain", "text": "主动消息"}]
        )
    )
    await entered.wait()
    await c.context.send_message(
        "custom-account:GroupMessage:999", MessageChain([Plain("其他普通消息")])
    )
    release.set()
    await pending
    assert [str(call.args[0]) for call in c.actual_send.call_args_list] == [
        "custom-account:GroupMessage:999",
        c.session,
    ]
    await c.runtime.close()
    assert c.context.send_message is c.actual_send


async def test_output_plugin_notification_to_another_target_keeps_original_behavior(
    contact_case,
):
    c = contact_case

    async def hook(event, kind):
        if kind == EventType.OnDecoratingResultEvent:
            await c.context.send_message(
                "custom-account:GroupMessage:999",
                MessageChain([Plain("插件自己的通知")]),
            )

    c.runtime.hook = hook
    wrapper = await c.begin(c.cron())
    await SendMessageToUserTool().call(
        wrapper, messages=[{"type": "plain", "text": "第一句。第二句！"}]
    )
    assert c.actual_send.await_count == 3
    facts = await c.runtime.state.facts(c.session)
    assert facts["unanswered_contacts"] == 1
    assert facts["last_result"]["sent_parts"] == 2
    await c.runtime.close()


async def test_unload_during_output_plugin_hook_blocks_later_direct_sends(contact_case):
    c = contact_case

    async def hook(event, kind):
        if kind == EventType.OnDecoratingResultEvent:
            await c.context.send_message(
                event.unified_msg_origin, MessageChain([Plain("已送达")])
            )
            await c.runtime.close()
            with pytest.raises(ContactStopped):
                await c.context.send_message(
                    event.unified_msg_origin, MessageChain([Plain("不能再发")])
                )

    c.runtime.hook = hook
    wrapper = await c.begin(c.cron())
    assert (
        await SendMessageToUserTool().call(
            wrapper, messages=[{"type": "plain", "text": "原始正文"}]
        )
    ).startswith("error:")
    c.actual_send.assert_awaited_once()
    assert c.context.send_message is c.actual_send


async def test_cancellation_is_not_lost_when_a_decoration_dispatcher_swallows_it(
    contact_case,
):
    c = contact_case
    entered = asyncio.Event()

    async def pending_send(*args):
        entered.set()
        await asyncio.Event().wait()

    async def hook(event, kind):
        if kind == EventType.OnDecoratingResultEvent:
            try:
                await c.context.send_message(
                    event.unified_msg_origin, MessageChain([Plain("未确认的段")])
                )
            except asyncio.CancelledError:
                return False

    c.actual_send.side_effect = pending_send
    c.runtime.hook = hook
    wrapper = await c.begin(c.cron())
    pending = asyncio.create_task(
        SendMessageToUserTool().call(
            wrapper, messages=[{"type": "plain", "text": "原始正文"}]
        )
    )
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    facts = await c.runtime.state.facts(c.session)
    assert facts["unanswered_contacts"] == 0
    assert facts["last_result"]["status"] == "unknown"
    await c.runtime.close()


async def test_silent_turn_and_group_presence_are_facts_not_unanswered_contacts(
    contact_case,
):
    c = contact_case
    event = c.cron()
    await c.begin(event)
    await c.runtime.finish(event)
    await c.runtime.observe(c.incoming("custom-account:GroupMessage:999"))
    facts = await c.runtime.state.facts(c.session)
    assert facts["unanswered_contacts"] == 0
    assert facts["last_seen_group"]["session"] == "custom-account:GroupMessage:999"
    assert facts["last_result"]["status"] == "silent"
    assert "text" not in str(c.store)
    await c.runtime.close()


async def test_interleaved_turns_count_once_each_and_restart_retains_facts(
    contact_case,
):
    from astrbot_plugin_krypton.contact_state import ContactState

    c = contact_case
    first = await c.begin(c.cron())
    second = await c.begin(c.cron())
    await SendMessageToUserTool().call(
        first, messages=[{"type": "plain", "text": "第一轮开头"}]
    )
    await SendMessageToUserTool().call(
        second, messages=[{"type": "plain", "text": "第二轮开头"}]
    )
    await SendMessageToUserTool().call(
        first, messages=[{"type": "plain", "text": "第一轮补充"}]
    )
    assert (await c.runtime.state.facts(c.session))["unanswered_contacts"] == 2
    restored = ContactState(c.runtime.state.load, c.runtime.state.save)
    assert (await restored.facts(c.session))["unanswered_contacts"] == 2
    await restored.observe(c.incoming(), {})
    assert (await restored.facts(c.session))["unanswered_contacts"] == 0
    await c.runtime.close()


@pytest.mark.parametrize("change", ["managed_off", "native_off"])
async def test_contact_switches_are_rechecked_between_segments(contact_case, change):
    c = contact_case
    event = c.cron()
    event.get_extra("cron_payload")["krypton"]["managed_private"] = True
    wrapper = await c.begin(event)

    async def pause(*args):
        if change == "managed_off":
            c.runtime.private._allowed = lambda *args: False
        else:
            c.context.get_config = lambda umo=None: {
                "provider_settings": {"proactive_capability": {"add_cron_tools": False}}
            }

    c.runtime.sleep = pause
    assert (
        await SendMessageToUserTool().call(
            wrapper, messages=[{"type": "plain", "text": "第一句。第二句！"}]
        )
    ).startswith("error:")
    c.actual_send.assert_awaited_once()
    await c.runtime.close()


async def test_contact_state_tool_enforces_cross_session_admin_permission(contact_case):
    c = contact_case
    event = c.cron()
    event.role = "member"
    wrapper = await c.begin(event)
    result = await GetContactStateTool(runtime=c.runtime).call(
        wrapper, session="custom-account:FriendMessage:654321"
    )
    assert "管理员" in result
    await c.runtime.close()


async def test_refreshing_owner_history_cannot_reenable_a_stale_contact_turn(
    contact_case,
):
    c = contact_case
    event = c.cron()
    wrapper = await c.begin(event)
    await c.runtime.observe(c.incoming())
    await c.runtime.context_viewed(event, c.session)
    result = await SendMessageToUserTool().call(
        wrapper, messages=[{"type": "plain", "text": "过期主动消息"}]
    )
    assert result.startswith("error:")
    c.actual_send.assert_not_awaited()
    assert (await c.runtime.state.facts(c.session))["last_result"][
        "status"
    ] == "cancelled"
    await c.runtime.close()


async def test_reply_during_native_agent_preparation_stops_the_old_turn(contact_case):
    from datetime import UTC, datetime, timedelta

    c = contact_case
    event = c.cron()
    event.set_extra(
        "cron_job",
        {"run_started_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
    )
    await c.runtime.observe(c.incoming())
    wrapper = await c.begin(event)
    result = await SendMessageToUserTool().call(
        wrapper, messages=[{"type": "plain", "text": "旧主动话题"}]
    )
    assert result.startswith("error:")
    c.actual_send.assert_not_awaited()
    await c.runtime.close()


def test_segment_extractor_keeps_capture_groups_gaps_code_and_original_chain():
    cfg = {"enable": True, "regex": r"(第二句。)", "words_count_threshold": 150}
    assert split_text("第一句。第二句。尾巴", cfg) == ["第一句。", "第二句。", "尾巴"]
    code = "```python\nprint('。')\n```"
    assert split_text(code, cfg) == [code]
    cfg["regex"] = "["
    assert split_text("保留原文。", cfg) == ["保留原文。"]
    chain = [Plain("第一。第二！")]
    cfg = {"enable": True, "split_mode": "words", "split_words": ["。", "！"]}
    assert len(build_parts(chain, cfg)) == 2
    assert chain[0].text == "第一。第二！"


async def test_real_native_runner_executes_contact_send_with_tools_and_hooks(
    contact_case,
):
    from astrbot.api import ToolSet
    from astrbot.core.agent.hooks import BaseAgentRunHooks
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.provider.entities import LLMResponse, ProviderRequest

    c = contact_case
    event = c.cron()

    class Hooks(BaseAgentRunHooks):
        async def on_agent_begin(self, context):
            await c.runtime.begin(event, context)

        async def on_tool_end(self, context, tool, args, result):
            c.runtime.reconcile_history(event)

        async def on_agent_done(self, context, response):
            await c.runtime.finish(event)

    provider = SimpleNamespace(
        provider_config={"id": "contact-test", "max_context_tokens": 0},
        text_chat=AsyncMock(
            side_effect=[
                LLMResponse(
                    role="assistant",
                    completion_text="",
                    tools_call_name=["send_message_to_user"],
                    tools_call_args=[
                        {
                            "messages": [
                                {"type": "plain", "text": "先聊一句。再聊一句！"}
                            ]
                        }
                    ],
                    tools_call_ids=["send-once"],
                ),
                LLMResponse(role="assistant", completion_text="本轮已完成"),
            ]
        ),
    )
    runner = ToolLoopAgentRunner()
    wrapper = ContextWrapper(context=SimpleNamespace(context=c.context, event=event))
    await runner.reset(
        provider=provider,
        request=ProviderRequest(
            prompt="自主联系", func_tool=ToolSet([SendMessageToUserTool()])
        ),
        run_context=wrapper,
        tool_executor=FunctionToolExecutor(),
        agent_hooks=Hooks(),
    )
    try:
        async for _ in runner.step_until_done(4):
            pass
        assert runner.state.name == "DONE"
        assert c.actual_send.await_count == 2
        assert (await c.runtime.state.facts(c.session))["unanswered_contacts"] == 1
    finally:
        await c.runtime.close()
