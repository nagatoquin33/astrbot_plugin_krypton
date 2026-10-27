import json
import time
from types import SimpleNamespace

import pytest
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot_plugin_krypton.activity import GroupActivity
from astrbot_plugin_krypton.chat_context import get_chat_context
from astrbot_plugin_krypton.platform_api import OneBotClient
from astrbot_plugin_krypton.tools import GetGroupActivityTool


async def test_activity_tracks_views_and_preserves_baseline(context, event, onebot):
    platform, state = onebot
    target = "custom-account:GroupMessage:123"
    await context.get_db().upsert_umo_alias(target, "owner", "测试群", None)
    store = {}

    async def load(key, default):
        return store.get(key, default)

    async def save(key, value):
        store[key] = value

    activity = GroupActivity(context, OneBotClient(), load, save)
    records = [
        {
            "message_id": i,
            "message_seq": i,
            "time": i,
            "sender": {"nickname": "某人"},
            "message": "内容",
        }
        for i in range(1, 4)
    ]
    state["get_group_msg_history"] = {"messages": records[:1]}
    first = await activity.list_activity(event)
    group = next(g for g in first["groups"] if g["session"] == target)
    assert "暂无基准" in group["status"]
    await get_chat_context(
        context, event, target, client=activity.client, activity=activity
    )
    state["get_group_msg_history"] = {"messages": records}
    restarted = GroupActivity(context, OneBotClient(), load, save)
    result = await restarted.list_activity(event)
    group = next(g for g in result["groups"] if g["session"] == target)
    assert group["new_messages"] == 2
    assert (await restarted.list_activity(event))["groups"] == result["groups"]
    await get_chat_context(
        context, event, target, client=activity.client, activity=restarted
    )
    assert (
        next(
            g
            for g in (await restarted.list_activity(event))["groups"]
            if g["session"] == target
        )["new_messages"]
        == 0
    )
    state["get_group_msg_history"] = {"messages": records[:1]}
    assert (
        "数量未知"
        in next(
            g
            for g in (await restarted.list_activity(event))["groups"]
            if g["session"] == target
        )["status"]
    )
    platform.bot.call_action.assert_awaited()


async def test_activity_requires_admin_and_uses_per_admin_markers(
    context, event, onebot
):
    store = {}

    async def load(key, default):
        return store.get(key, default)

    async def save(key, value):
        store[key] = value

    activity = GroupActivity(context, OneBotClient(), load, save)
    await activity.mark_viewed(
        event, "custom-account:GroupMessage:123", [{"message_id": 5}]
    )
    event.get_sender_id = lambda: "other-admin"
    assert (
        "other-admin\ncustom-account:GroupMessage:123"
        not in store["group_activity_markers"]
    )
    event.role = "member"
    wrapper = SimpleNamespace(context=SimpleNamespace(event=event))
    result = json.loads(await GetGroupActivityTool(activity=activity).call(wrapper))
    assert "管理员" in result["error"]
    with pytest.raises(PermissionError):
        await activity.list_activity(event)


async def test_scheduled_agent_uses_task_creator_baseline(context, event, onebot):
    store = {}

    async def load(key, default):
        return store.get(key, default)

    async def save(key, value):
        store[key] = value

    activity = GroupActivity(context, OneBotClient(), load, save)
    target = "custom-account:GroupMessage:123"
    await context.get_db().upsert_umo_alias(target, "owner", "测试群", None)
    await activity.mark_viewed(event, target, [{"message_id": 1}])
    onebot[1]["get_group_msg_history"] = {
        "messages": [{"message_id": 1, "time": 1}, {"message_id": 2, "time": 2}]
    }
    cron = CronMessageEvent(
        context=context,
        session=MessageSession.from_str("test:FriendMessage:owner"),
        message="继续联系",
        message_type=MessageType.FRIEND_MESSAGE,
        extras={"cron_payload": {"sender_id": "owner"}},
    )
    cron.role = "admin"
    result = await activity.list_activity(cron)
    assert (
        next(g for g in result["groups"] if g["session"] == target)["new_messages"] == 1
    )


async def test_first_query_sorts_all_groups_before_pagination(context, event, onebot):
    platform, _ = onebot
    store = {}

    async def load(key, default):
        return store.get(key, default)

    async def save(key, value):
        store[key] = value

    now = int(time.time())

    async def history(action, **params):
        counts = {"1": 1, "2": 3, "3": 2}
        return {
            "messages": [
                {"message_id": i, "time": now - i}
                for i in range(counts[params["group_id"]])
            ]
        }

    platform.bot.call_action.side_effect = history
    for group in ("1", "2", "3"):
        await context.get_db().upsert_umo_alias(
            f"custom-account:GroupMessage:{group}", "owner", f"群{group}", None
        )
    activity = GroupActivity(context, OneBotClient(), load, save)
    first = await activity.list_activity(event, limit=1)
    second = await activity.list_activity(event, limit=1, offset=first["next_offset"])
    assert first["groups"][0]["session"].endswith(":2")
    assert first["groups"][0]["recent_messages_visible"] == 3
    assert second["groups"][0]["session"].endswith(":3")
    assert "暂无基准" in first["groups"][0]["status"]
    assert store == {}


async def test_markers_avoid_older_view_overwrite_and_support_legacy(
    context, event, onebot
):
    target = "custom-account:GroupMessage:123"
    key = f"owner\n{target}"
    store = {"group_activity_markers": {key: "1"}}

    async def load(key, default):
        return store.get(key, default)

    async def save(key, value):
        store[key] = value

    await context.get_db().upsert_umo_alias(target, "owner", "测试群", None)
    onebot[1]["get_group_msg_history"] = {
        "messages": [{"message_id": 1, "time": 1}, {"message_id": 2, "time": 2}]
    }
    activity = GroupActivity(context, OneBotClient(), load, save)
    result = await activity.list_activity(event)
    assert (
        next(g for g in result["groups"] if g["session"] == target)["new_messages"] == 1
    )
    await activity.mark_viewed(event, target, [{"message_id": 2, "time": 2}])
    await activity.mark_viewed(event, target, [{"message_id": 1, "time": 1}])
    assert store["group_activity_markers"][key]["id"] == "2"


async def test_recent_window_and_missing_message_timestamps(context, event, onebot):
    target = "custom-account:GroupMessage:123"
    await context.get_db().upsert_umo_alias(target, "owner", "测试群", None)
    now = time.time()
    onebot[1]["get_group_msg_history"] = {
        "messages": [
            {"message_id": 1, "time": now - 7200},
            {"message_id": 2, "time": now - 60},
            {"message_id": 3},
        ]
    }

    async def load(key, default):
        return default

    async def save(key, value):
        raise AssertionError("状态查询不能写入基准")

    activity = GroupActivity(context, OneBotClient(), load, save)
    result = await activity.list_activity(event, hours=1)
    group = next(g for g in result["groups"] if g["session"] == target)
    assert result["recent_hours"] == 1
    assert group["recent_messages_visible"] == 1
    assert "缺少时间" in group["recent_note"]


async def test_new_messages_rank_above_unread_unknown_and_already_viewed_groups(
    context, event, onebot
):
    platform, _ = onebot
    now = int(time.time())
    histories = {
        "1": [{"message_id": i, "time": now - 100 + i} for i in range(1, 101)],
        "2": [{"message_id": i, "time": now - 10 + i} for i in range(6)],
        "3": [{"message_id": i, "time": now - 20 + i} for i in range(20)],
    }
    markers = {
        "owner\ncustom-account:GroupMessage:1": "100",
        "owner\ncustom-account:GroupMessage:2": "0",
    }

    async def load(key, default):
        return markers

    async def save(key, value):
        raise AssertionError("查询不能写入基准")

    async def history(action, **params):
        return {"messages": histories[params["group_id"]]}

    platform.bot.call_action.side_effect = history
    for group in histories:
        await context.get_db().upsert_umo_alias(
            f"custom-account:GroupMessage:{group}", "owner", f"群{group}", None
        )
    activity = GroupActivity(context, OneBotClient(), load, save)
    first = await activity.list_activity(event, limit=1)
    assert first["groups"][0]["session"].endswith(":2")
    assert first["groups"][0]["new_messages"] == 5
    second = await activity.list_activity(event, limit=1, offset=1)
    assert second["groups"][0]["session"].endswith(":3")
    third = await activity.list_activity(event, limit=1, offset=2)
    assert third["groups"][0]["session"].endswith(":1")
    assert third["groups"][0]["new_messages"] == 0


@pytest.mark.parametrize("failure", [False, True])
async def test_isolated_sessions_share_history_fetch_but_keep_independent_markers(
    context, event, onebot, failure
):
    platform, state = onebot
    targets = [f"custom-account:GroupMessage:{i}_123" for i in range(12)]
    now = int(time.time())
    state["get_group_msg_history"] = {
        "messages": [{"message_id": i, "time": now - 10 + i} for i in range(4)]
    }
    if failure:
        state["get_group_msg_history"] = RuntimeError("平台断开")
    markers = {f"owner\n{targets[0]}": "1", f"owner\n{targets[1]}": "3"}

    async def load(key, default):
        return markers

    async def save(key, value):
        raise AssertionError("查询不能写入基准")

    for target in targets:
        await context.get_db().upsert_umo_alias(target, "owner", "同一群", None)
    activity = GroupActivity(context, OneBotClient(), load, save)
    result = await activity.list_activity(event)
    rows = {g["session"]: g for g in result["groups"]}
    if failure:
        assert all("数量未知" in rows[target]["status"] for target in targets)
    else:
        assert rows[targets[0]]["new_messages"] == 2
        assert rows[targets[1]]["new_messages"] == 0
        assert "暂无基准" in rows[targets[2]]["status"]
    platform.bot.call_action.assert_awaited_once_with(
        "get_group_msg_history", group_id="123", count=100
    )


async def test_same_group_id_on_different_accounts_keeps_separate_histories(
    context, event, onebot
):
    from unittest.mock import AsyncMock

    from astrbot.core.platform.platform_metadata import PlatformMetadata

    first, state = onebot
    now = int(time.time())
    state["get_group_msg_history"] = {"messages": [{"message_id": 1, "time": now}]}
    meta = PlatformMetadata(
        name="aiocqhttp", description="另一个账号", id="second-account"
    )
    second = SimpleNamespace(
        meta=lambda: meta,
        bot=SimpleNamespace(call_action=AsyncMock(return_value={"messages": []})),
    )
    context.platform_manager.platform_insts.append(second)

    async def load(key, default):
        return default

    async def save(key, value):
        raise AssertionError("查询不能写入基准")

    for account in ("custom-account", "second-account"):
        await context.get_db().upsert_umo_alias(
            f"{account}:GroupMessage:123", "owner", "同群号", None
        )
    activity = GroupActivity(context, OneBotClient(), load, save)
    rows = {g["session"]: g for g in (await activity.list_activity(event))["groups"]}
    assert rows["custom-account:GroupMessage:123"]["recent_messages_visible"] == 1
    assert rows["second-account:GroupMessage:123"]["recent_messages_visible"] == 0
    first.bot.call_action.assert_awaited_once()
    second.bot.call_action.assert_awaited_once()
