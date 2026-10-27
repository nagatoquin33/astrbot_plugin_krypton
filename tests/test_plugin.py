from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot.api import FunctionTool, ToolSet
from astrbot.core.provider.entities import ProviderRequest
from astrbot_plugin_krypton.main import KryptonPlugin
from astrbot_plugin_krypton.tasks import TaskService
from astrbot_plugin_krypton.tools import (
    FutureTaskPlusTool,
    GetChatContextTool,
    ListChatSessionsTool,
)


async def test_request_prefers_enhanced_tool_without_modifying_global_builtin(
    context, event
):
    registered = []
    context.add_llm_tools = lambda *tools: registered.extend(tools)
    plugin = KryptonPlugin(context)
    assert {tool.name for tool in registered} == {
        "future_task_plus",
        "list_chat_sessions",
        "get_chat_context",
        "get_group_activity",
    }
    native = FunctionTool(
        name="future_task", description="native", parameters={"type": "object"}
    )
    original_history = [{"role": "user", "content": "原始对话"}]
    req = ProviderRequest(
        func_tool=ToolSet([native, *registered]),
        system_prompt="原有人设与系统提示",
        prompt="原始用户输入",
        contexts=original_history.copy(),
    )
    await plugin.prepare_request(event, req)
    assert req.func_tool.get_tool("future_task") is None
    assert native.name == "future_task" and native.active
    assert req.system_prompt == "原有人设与系统提示"
    assert req.prompt == "原始用户输入"
    assert req.contexts == original_history
    context.llm_generate.assert_not_awaited()


async def test_request_preserves_native_when_plugin_tool_not_available(context, event):
    plugin = KryptonPlugin(context)
    native = FunctionTool(
        name="future_task", description="native", parameters={"type": "object"}
    )
    req = ProviderRequest(func_tool=ToolSet([native]))
    await plugin.prepare_request(event, req)
    assert req.func_tool.get_tool("future_task") is native
    assert req.system_prompt == ""
    disabled = FutureTaskPlusTool(service=TaskService(context), active=False)
    req.func_tool.add_tool(disabled)
    await plugin.prepare_request(event, req)
    assert req.func_tool.get_tool("future_task") is native


@pytest.mark.parametrize("enabled", [False, True])
async def test_enhanced_tool_follows_native_switch_and_keeps_session_discovery(
    context, event, enabled
):
    context.get_config = lambda umo=None: {
        "provider_settings": {"proactive_capability": {"add_cron_tools": enabled}}
    }
    plugin = KryptonPlugin(context)
    req = ProviderRequest(
        func_tool=context.get_llm_tool_manager().get_full_tool_set(),
        system_prompt="原始提示",
    )
    await plugin.prepare_request(event, req)
    assert (req.func_tool.get_tool("future_task_plus") is not None) is enabled
    assert req.func_tool.get_tool("list_chat_sessions") is not None
    assert req.func_tool.get_tool("get_chat_context") is not None
    assert req.system_prompt == "原始提示"
    context.llm_generate.assert_not_awaited()


def test_registered_tools_have_plugin_identity_for_unload_and_session_filtering(
    context,
):
    from astrbot.core.star.context import Context

    tools = [
        ListChatSessionsTool(plugin_context=context),
        GetChatContextTool(plugin_context=context),
        FutureTaskPlusTool(service=TaskService(context)),
    ]
    ctx = object.__new__(Context)
    ctx.provider_manager = SimpleNamespace(llm_tools=SimpleNamespace(func_list=[]))
    ctx.add_llm_tools(*tools)
    assert len(ctx.provider_manager.llm_tools.func_list) == 3
    assert all("astrbot_plugin_krypton" in tool.handler_module_path for tool in tools)


def test_metadata_is_accepted_by_astrbot_loader():
    from astrbot.core.star.star_manager import PluginManager

    metadata = PluginManager._load_plugin_metadata(
        str(Path(__file__).resolve().parents[1])
    )
    assert metadata.name == "astrbot_plugin_krypton"
    assert PluginManager._validate_astrbot_version_specifier(metadata.astrbot_version)[
        0
    ]
