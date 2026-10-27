import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.tools.message_tools import SendMessageToUserTool
from astrbot_plugin_krypton.chat_context import get_chat_context, message_text
from astrbot_plugin_krypton.main import KryptonPlugin
from astrbot_plugin_krypton.platform_api import OneBotClient
from astrbot_plugin_krypton.sessions import list_sessions
from astrbot_plugin_krypton.tools import GetChatContextTool


def platform_message(text, *, timestamp=1700000000, sender_id="member", **extra):
    return {
        "time": timestamp,
        "message_id": str(timestamp),
        "self_id": "bot",
        "sender": {"user_id": sender_id, "nickname": "群成员", "role": "member"},
        "message": [{"type": "text", "data": {"text": text}}],
        **extra,
    }


async def local_message(context, session, text):
    return await context.get_db().insert_platform_message_history(
        platform_id=session.split(":", 1)[0],
        user_id=session,
        content={"type": "user", "message": [{"type": "plain", "text": text}]},
        sender_id="local-member",
        sender_name="本地成员",
    )


async def test_live_group_context_has_only_sender_time_text_and_media_placeholders(
    context, event, onebot
):
    platform, state = onebot
    state["get_group_msg_history"] = {
        "messages": [
            platform_message("新话题", timestamp=1700000010),
            platform_message(
                "旧话题",
                timestamp=1700000000,
                sender_id="bot",
                message=[
                    {
                        "type": "image",
                        "data": {
                            "url": "https://example.invalid/image?rkey=private-token"
                        },
                    },
                    {"type": "record", "data": {"file": "private-file-path"}},
                ],
            ),
        ]
    }
    result = await get_chat_context(context, event, "custom-account:GroupMessage:123")
    assert set(result) == {"messages"}
    assert all(set(row) == {"sender", "time", "text"} for row in result["messages"])
    assert [message["text"] for message in result["messages"]] == [
        "[图片] [语音]",
        "新话题",
    ]
    assert result["messages"][1]["sender"] == "群成员"
    assert result["messages"][1]["time"] == datetime.fromtimestamp(
        1700000010, UTC
    ).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    assert "private-token" not in json.dumps(
        result
    ) and "private-file-path" not in json.dumps(result)
    platform.bot.call_action.assert_awaited_once_with(
        "get_group_msg_history", group_id="123", count=10
    )


async def test_private_history_uses_friend_api_and_caps_count(context, event, onebot):
    platform, state = onebot
    state["get_friend_msg_history"] = {
        "status": "ok",
        "retcode": 0,
        "data": {
            "messages": [
                platform_message(str(index), timestamp=1700000000 + index)
                for index in range(30)
            ]
        },
    }
    result = await get_chat_context(
        context, event, "custom-account:FriendMessage:456", limit=100
    )
    assert len(result["messages"]) == 20
    assert result["messages"][0]["text"] == "10"
    platform.bot.call_action.assert_awaited_once_with(
        "get_friend_msg_history", user_id="456", count=20
    )


async def test_history_is_refetched_and_empty_live_result_does_not_resurrect_local_data(
    context, event, onebot
):
    platform, state = onebot
    await local_message(context, "custom-account:GroupMessage:123", "过时记录")
    state["get_group_msg_history"] = {"messages": [platform_message("第一次查询")]}
    first = await get_chat_context(context, event, "custom-account:GroupMessage:123")
    assert first["messages"][0]["text"] == "第一次查询"
    state["get_group_msg_history"] = {"messages": []}
    second = await get_chat_context(context, event, "custom-account:GroupMessage:123")
    assert second == {"messages": []}
    assert platform.bot.call_action.await_count == 2


async def test_failed_platform_falls_back_to_exact_isolated_session_only(
    context, event, onebot
):
    platform, state = onebot
    state["get_group_msg_history"] = RuntimeError("history unsupported")
    context.get_config = lambda umo=None: {
        "provider_ltm_settings": {"group_message_history_enable": True}
    }
    requested = "custom-account:GroupMessage:alice_123"
    await local_message(context, requested, "Alice 会话的现场记录")
    await local_message(
        context, "custom-account:GroupMessage:bob_123", "Bob 的独立上下文"
    )
    await local_message(context, "custom-account:GroupMessage:123", "群级独立上下文")
    result = await get_chat_context(context, event, requested)
    assert result["note"] == "仅本地留存记录，可能不完整。"
    assert all(set(row) == {"sender", "time", "text"} for row in result["messages"])
    assert [row["text"] for row in result["messages"]] == ["Alice 会话的现场记录"]
    assert "history unsupported" not in json.dumps(result)
    assert result["messages"][0]["time"] is not None
    platform.bot.call_action.assert_awaited_once_with(
        "get_group_msg_history", group_id="123", count=10
    )


async def test_disabled_local_recording_is_not_bypassed_when_platform_fails(
    context, event, onebot
):
    _, state = onebot
    state["get_group_msg_history"] = RuntimeError("unavailable")
    context.message_history_manager.get = AsyncMock(
        side_effect=AssertionError("不应读取停用的记录")
    )
    result = await get_chat_context(context, event, "custom-account:GroupMessage:123")
    assert "error" in result and not result["messages"]
    assert set(result) == {"messages", "error"}
    context.message_history_manager.get.assert_not_awaited()


async def test_current_session_member_can_read_but_cross_session_denied_before_any_lookup(
    event,
):
    event.role = "member"
    tool = GetChatContextTool(plugin_context=object())
    wrapper = SimpleNamespace(context=SimpleNamespace(event=event))
    result = json.loads(
        await tool.call(wrapper, session="custom-account:GroupMessage:123")
    )
    assert "管理员" in result["error"]


async def test_current_session_member_can_query_live_context(context, event, onebot):
    _, state = onebot
    event.role = "member"
    event.unified_msg_origin = "custom-account:GroupMessage:123"
    state["get_group_msg_history"] = {"messages": [platform_message("本群话题")]}
    result = await get_chat_context(context, event, event.unified_msg_origin)
    assert result["messages"][0]["text"] == "本群话题"


async def test_api_timeout_degrades_without_waiting_for_adapter_default_timeout(
    context, event, onebot, monkeypatch
):
    from unittest.mock import Mock

    import astrbot_plugin_krypton.chat_context as chat_context

    log = Mock()
    monkeypatch.setattr(chat_context.logger, "warning", log)
    platform, _ = onebot

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    platform.bot.call_action.side_effect = hang
    context.get_config = lambda umo=None: {
        "provider_ltm_settings": {"group_message_history_enable": True}
    }
    await local_message(context, "custom-account:GroupMessage:123", "本地留存")
    result = await get_chat_context(
        context,
        event,
        "custom-account:GroupMessage:123",
        client=OneBotClient(timeout=0.01),
    )
    assert "本地" in result["note"]
    assert "TimeoutError" in str(log.call_args)
    assert "TimeoutError" not in json.dumps(result)


async def test_malformed_platform_page_does_not_mix_partial_data_with_local_records(
    context, event, onebot
):
    _, state = onebot
    context.get_config = lambda umo=None: {
        "provider_ltm_settings": {"group_message_history_enable": True}
    }
    await local_message(context, "custom-account:GroupMessage:123", "完整本地记录")
    state["get_group_msg_history"] = {
        "messages": [
            platform_message("不应混入的残缺平台记录"),
            platform_message("坏记录", timestamp=99999999999999999),
        ]
    }
    result = await get_chat_context(context, event, "custom-account:GroupMessage:123")
    assert "本地" in result["note"]
    assert [row["text"] for row in result["messages"]] == ["完整本地记录"]


async def test_unsupported_platform_uses_real_local_messages_not_llm_conversation(
    context, event
):
    context.get_config = lambda umo=None: {
        "provider_ltm_settings": {"group_message_history_enable": True}
    }
    await local_message(context, event.unified_msg_origin, "原始群消息")
    result = await get_chat_context(context, event, event.unified_msg_origin)
    assert "本地" in result["note"]
    assert [row["text"] for row in result["messages"]] == ["原始群消息"]


def test_cq_media_string_does_not_expose_media_urls():
    assert (
        message_text(
            "看这个[CQ:image,file=secret-path,url=https://example.invalid/?rkey=secret]"
        )
        == "看这个[图片]"
    )


async def test_sender_control_payload_is_removed_without_damaging_normal_names(
    context, event, onebot
):
    _, state = onebot
    state["get_group_msg_history"] = {
        "messages": [
            platform_message(
                "金",
                sender={
                    "user_id": "123",
                    "nickname": "\n\x17\x12\x15内向纯情小男高\n\t\n\x07$ÿĀ\x11\x10\x10\x00",
                },
            ),
            platform_message(
                "正常名字",
                timestamp=1700000001,
                sender={"user_id": "456", "nickname": "九曜 🧑\u200d💻 $ÿĀ"},
            ),
        ]
    }
    result = await get_chat_context(context, event, "custom-account:GroupMessage:123")
    assert result["messages"][0]["sender"] == "内向纯情小男高"
    assert result["messages"][1]["sender"] == "九曜 🧑\u200d💻 $ÿĀ"
    assert all(set(row) == {"sender", "time", "text"} for row in result["messages"])


@pytest.mark.parametrize("timezone", ["Asia/Shanghai", "America/New_York"])
async def test_recent_message_time_uses_calling_session_timezone(
    context, event, onebot, timezone
):
    _, state = onebot
    context.get_config = lambda umo=None: {"timezone": timezone}
    sent = datetime.now(UTC).replace(month=10, day=23, hour=2, minute=37, second=35)
    state["get_group_msg_history"] = {
        "messages": [platform_message("时间展示", timestamp=sent.timestamp())]
    }
    result = await get_chat_context(context, event, "custom-account:GroupMessage:123")
    assert result["messages"][0]["time"] == sent.astimezone(
        ZoneInfo(timezone)
    ).strftime("%m-%d %H:%M")


async def test_mentions_use_names_or_placeholders_instead_of_exposing_ids(
    context, event, onebot
):
    _, state = onebot
    state["get_group_msg_history"] = {
        "messages": [
            platform_message(
                "上一句",
                timestamp=1700000000,
                sender={"user_id": "123456", "nickname": "小海"},
            ),
            platform_message(
                "回复",
                timestamp=1700000001,
                message=[
                    {"type": "reply", "data": {"id": "message-id"}},
                    {"type": "at", "data": {"qq": "123456"}},
                    {"type": "at", "data": {"qq": "654321"}},
                    {"type": "at", "data": {"qq": "all"}},
                    {"type": "text", "data": {"text": "正文数字 123456 保留"}},
                ],
            ),
        ]
    }
    result = await get_chat_context(context, event, "custom-account:GroupMessage:123")
    assert (
        result["messages"][1]["text"]
        == "[回复] @小海 @某人 @全体成员 正文数字 123456 保留"
    )
    assert (
        message_text("[CQ:at,qq=123456] [CQ:at,qq=all]", {"123456": "小海"})
        == "@小海 @全体成员"
    )


async def test_long_text_uses_ellipsis_without_per_message_truncation_flags(
    context, event, onebot
):
    _, state = onebot
    state["get_group_msg_history"] = {"messages": [platform_message("长" * 2100)]}
    result = await get_chat_context(context, event, "custom-account:GroupMessage:123")
    row = result["messages"][0]
    assert set(row) == {"sender", "time", "text"}
    assert row["text"] == "长" * 2000 + "…"


async def test_known_target_task_wakes_there_reads_latest_context_and_delivers_there(
    context, event, onebot
):
    _, state = onebot
    await context.get_db().upsert_umo_alias(
        "custom-account:GroupMessage:123", "owner", "已知群", None
    )
    KryptonPlugin(context)
    target = (await list_sessions(context, event, search="已知群"))["sessions"][0][
        "session"
    ]
    wrapper = SimpleNamespace(context=SimpleNamespace(context=context, event=event))
    task_tool = (
        context.get_llm_tool_manager().get_full_tool_set().get_tool("future_task_plus")
    )
    created = json.loads(
        await task_tool.call(
            wrapper,
            action="create",
            session=target,
            note="了解目标群现场，再决定如何交流",
            background="来源群在讨论发布情况，希望了解目标群的想法。",
            run_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        )
    )
    job_id = created["task"]["job_id"]
    saved = await context.get_db().get_cron_job(job_id)
    assert f"任务来源会话：{event.unified_msg_origin}" in saved.payload["note"]
    assert "来源群在讨论发布情况" in saved.payload["note"]
    assert "主动执行指引" not in saved.payload["note"]
    state["get_group_msg_history"] = {
        "messages": [platform_message("执行前现场话题已经变化")]
    }

    async def wake(**kwargs):
        session = MessageSession.from_str(kwargs["session_str"])
        assert str(session) == target
        cron_event = CronMessageEvent(
            context=context,
            session=session,
            message=kwargs["message"],
            extras=kwargs["extras"],
            message_type=session.message_type,
        )
        local_wrapper = SimpleNamespace(
            context=SimpleNamespace(context=context, event=cron_event)
        )
        history_tool = (
            context.get_llm_tool_manager()
            .get_full_tool_set()
            .get_tool("get_chat_context")
        )
        live = json.loads(await history_tool.call(local_wrapper, session=target))
        assert live["messages"][0]["text"] == "执行前现场话题已经变化"
        sent = await SendMessageToUserTool().call(
            local_wrapper, messages=[{"type": "plain", "text": "脚本化执行结果"}]
        )
        assert sent == f"Message sent to session {target}"

    context.cron_manager._woke_main_agent = wake
    await context.cron_manager._run_job(job_id)
    assert await context.get_db().get_cron_job(job_id) is None
    assert str(context.send_message.call_args.args[0]) == target
    assert event._has_send_oper is False
    context.llm_generate.assert_not_awaited()
