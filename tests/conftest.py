"""使用本地源码或已安装 AstrBot，数据库始终放在临时目录。"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
SOURCE = Path(os.environ.get("ASTRBOT_SOURCE", str(ROOT.parent / "AstrBot")))
if (SOURCE / "astrbot").is_dir():
    sys.path.insert(0, str(SOURCE))

from astrbot.core.cron.manager import CronJobManager  # noqa: E402
from astrbot.core.db.sqlite import SQLiteDatabase  # noqa: E402
from astrbot.core.platform.message_type import MessageType  # noqa: E402
from astrbot.core.platform.platform_metadata import PlatformMetadata  # noqa: E402
from astrbot.core.platform_message_history_mgr import (  # noqa: E402
    PlatformMessageHistoryManager,
)
from astrbot.core.provider.func_tool_manager import FunctionToolManager  # noqa: E402
from astrbot.core.star.context import Context  # noqa: E402


@pytest.fixture
def event():
    extras = {}
    result = SimpleNamespace(
        unified_msg_origin="test:GroupMessage:home",
        role="admin",
        _has_send_oper=False,
        get_sender_id=lambda: "owner",
        get_message_type=lambda: MessageType.GROUP_MESSAGE,
    )
    result.get_extra = lambda key, default=None: extras.get(key, default)
    result.set_extra = lambda key, value: extras.__setitem__(key, value)
    return result


@pytest_asyncio.fixture
async def context(tmp_path):
    db = SQLiteDatabase(str(tmp_path / "tasks.db"))
    await db.initialize()
    meta = PlatformMetadata(name="test", description="Test", id="test")
    platform = SimpleNamespace(meta=lambda: meta)
    ctx = SimpleNamespace(
        get_db=lambda: db,
        get_config=lambda umo=None: {"timezone": "Asia/Shanghai"},
        platform_manager=SimpleNamespace(platform_insts=[platform]),
        get_platform_inst=lambda platform_id: (
            platform if platform_id == "test" else None
        ),
        send_message=AsyncMock(return_value=True),
    )
    manager = CronJobManager(db)
    tools = FunctionToolManager()
    tools._check_tool_permission = AsyncMock(return_value=None)
    ctx.provider_manager = SimpleNamespace(llm_tools=tools)
    ctx.add_llm_tools = lambda *items: Context.add_llm_tools(ctx, *items)
    ctx.get_llm_tool_manager = lambda: tools
    ctx.message_history_manager = PlatformMessageHistoryManager(db)
    ctx.llm_generate = AsyncMock(side_effect=AssertionError("插件不应额外调用模型"))
    ctx.cron_manager = manager
    await manager.start(ctx)
    try:
        yield ctx
    finally:
        await manager.shutdown()
        await db.engine.dispose()


@pytest.fixture
def onebot(context):
    """安装可控的 OneBot 适配器替身，不连接真实平台。"""
    state = {
        "get_group_msg_history": {"messages": []},
        "get_friend_msg_history": {"messages": []},
    }

    async def call(action, **params):
        value = state[action]
        if isinstance(value, Exception):
            raise value
        return value

    meta = PlatformMetadata(
        name="aiocqhttp", description="OneBot test", id="custom-account"
    )
    platform = SimpleNamespace(
        meta=lambda: meta, bot=SimpleNamespace(call_action=AsyncMock(side_effect=call))
    )
    context.platform_manager.platform_insts.append(platform)
    context.get_platform_inst = lambda platform_id: next(
        (
            p
            for p in context.platform_manager.platform_insts
            if p.meta().id == platform_id
        ),
        None,
    )
    return platform, state
