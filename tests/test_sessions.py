import json
from types import SimpleNamespace

import pytest
from astrbot.core.db.po import ConversationV2
from astrbot.core.tools.message_tools import SendMessageToUserTool
from astrbot_plugin_krypton.sessions import list_sessions
from astrbot_plugin_krypton.tools import ListChatSessionsTool


async def test_discover_names_aliases_legacy_and_send(context, event):
    db = context.get_db()
    await db.upsert_umo_alias("test:GroupMessage:target", "owner", "开发群", "工作群")
    async with db.get_db() as session:
        session.add(
            ConversationV2(user_id="test:GroupMessage:target", platform_id="test")
        )
        session.add(
            ConversationV2(user_id="test:FriendMessage:legacy", platform_id="test")
        )
        session.add(ConversationV2(user_id="malformed", platform_id="test"))
        session.add(
            ConversationV2(user_id="offline:FriendMessage:old", platform_id="offline")
        )
        await session.commit()

    result = await list_sessions(context, event, search="工作群")
    target = result["sessions"][0]["session"]
    assert target == "test:GroupMessage:target"
    assert result["total"] == 1
    assert (await list_sessions(context, event, search="开发群"))["total"] == 1
    page = await list_sessions(context, event, limit=1)
    assert page["total"] == 3
    assert page["next_offset"] == 1
    all_rows = (await list_sessions(context, event))["sessions"]
    assert any(row["session"] == "test:FriendMessage:legacy" for row in all_rows)

    wrapper = SimpleNamespace(context=SimpleNamespace(context=context, event=event))
    sent = await SendMessageToUserTool().call(
        wrapper, session=target, messages=[{"type": "plain", "text": "跨会话验证"}]
    )
    assert sent == f"Message sent to session {target}"
    assert str(context.send_message.call_args.args[0]) == target
    assert event._has_send_oper is False


async def test_duplicate_names_are_not_implicitly_resolved(context, event):
    for target in ("one", "two"):
        await context.get_db().upsert_umo_alias(
            f"test:GroupMessage:{target}", "owner", "同名群", None
        )
    result = await list_sessions(context, event, search="同名群")
    assert result["total"] == 2
    assert len({row["session"] for row in result["sessions"]}) == 2


async def test_discovery_denies_members_before_accessing_database(event):
    event.role = "member"
    tool = ListChatSessionsTool(plugin_context=object())
    result = json.loads(
        await tool.call(SimpleNamespace(context=SimpleNamespace(event=event)))
    )
    assert "管理员" in result["error"]


@pytest.mark.parametrize("field", ["offset", "limit"])
async def test_bad_pagination_returns_tool_error(context, event, field):
    tool = ListChatSessionsTool(plugin_context=context)
    wrapper = SimpleNamespace(context=SimpleNamespace(event=event))
    assert "error" in json.loads(await tool.call(wrapper, **{field: "bad"}))


async def test_listing_stays_local_and_does_not_enumerate_platform_contacts(
    context, event, onebot
):
    platform, _ = onebot
    await context.get_db().upsert_umo_alias(
        "custom-account:GroupMessage:123", "owner", "已知群", None
    )
    result = await list_sessions(context, event, search="已知群")
    assert result["sessions"][0]["session"] == "custom-account:GroupMessage:123"
    platform.bot.call_action.assert_not_awaited()


async def test_response_omits_duplicate_fields_and_id_only_names_without_losing_targets(
    context, event
):
    await context.get_db().upsert_umo_alias(
        "test:FriendMessage:123", "owner", "123", None
    )
    await context.get_db().upsert_umo_alias(
        "test:FriendMessage:456", "owner", "真实昵称", None
    )
    result = await list_sessions(context, event)
    rows = {row["session"]: row for row in result["sessions"]}
    assert rows["test:FriendMessage:123"] == {"session": "test:FriendMessage:123"}
    assert rows["test:FriendMessage:456"] == {
        "session": "test:FriendMessage:456",
        "name": "真实昵称",
    }
    assert rows[event.unified_msg_origin] == {
        "session": event.unified_msg_origin,
        "is_current": True,
    }
    found = await list_sessions(context, event, search="123")
    assert (
        found["total"] == 1
        and found["sessions"][0]["session"] == "test:FriendMessage:123"
    )


async def test_names_are_cleaned_for_display_and_search_while_emoji_are_preserved(
    context, event
):
    await context.get_db().upsert_umo_alias(
        "test:FriendMessage:123",
        "owner",
        "\u2067暗月寺华月\u1160\u2067~喵\u2067\u202d",
        None,
    )
    await context.get_db().upsert_umo_alias(
        "test:FriendMessage:456", "owner", "原昵称", "  九曜\n\t 🧑\u200d💻\u200b  "
    )
    await context.get_db().upsert_umo_alias(
        "test:FriendMessage:789", "owner", "33", None
    )
    rows = {
        row["session"]: row for row in (await list_sessions(context, event))["sessions"]
    }
    assert rows["test:FriendMessage:123"]["name"] == "暗月寺华月~喵"
    assert rows["test:FriendMessage:456"]["name"] == "九曜 🧑\u200d💻"
    assert rows["test:FriendMessage:789"]["name"] == "33"
    assert (await list_sessions(context, event, search="华月~喵"))["total"] == 1
    assert (await list_sessions(context, event, search="原昵称"))["total"] == 1


async def test_current_and_named_sessions_sort_before_unnamed_across_pages(
    context, event
):
    event.unified_msg_origin = "test:FriendMessage:003"
    for value in ("001", "002", "003"):
        await context.get_db().upsert_umo_alias(
            f"test:FriendMessage:{value}", "owner", value, None
        )
    await context.get_db().upsert_umo_alias(
        "test:FriendMessage:zzz", "owner", "有名朋友", None
    )
    await context.get_db().upsert_umo_alias(
        "test:GroupMessage:zzz", "owner", "有名群聊", None
    )
    first = await list_sessions(context, event, limit=2)
    second = await list_sessions(context, event, limit=2, offset=first["next_offset"])
    final = await list_sessions(context, event, limit=2, offset=second["next_offset"])
    rows = first["sessions"] + second["sessions"] + final["sessions"]
    assert first["total"] == 5
    assert rows[0]["session"] == event.unified_msg_origin
    assert [row["name"] for row in rows[1:3]] == ["有名朋友", "有名群聊"]
    assert len({row["session"] for row in rows}) == 5 and final["next_offset"] is None
