"""可选联测：直接运行相邻 OutputPro 源码中的分段步骤。"""

import importlib
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from astrbot.api.message_components import Plain
from astrbot.core.star.star_handler import EventType
from astrbot.core.tools.message_tools import SendMessageToUserTool


async def test_actual_outputpro_split_step_is_routed_and_counted_once(contact_case):
    root = Path(
        os.environ.get(
            "OUTPUTPRO_SOURCE",
            str(Path(__file__).resolve().parents[2] / "astrbot_plugin_outputpro"),
        )
    )
    if not (root / "core" / "step" / "split.py").is_file():
        pytest.skip("未提供可选 OutputPro 源码")
    # 仅装载真实分段模块，避免导入与本次联测无关的渲染、转发等可选依赖。
    package = "_krypton_outputpro_compat"
    namespace = ModuleType(package)
    namespace.__path__ = [str(root / "core")]
    steps = ModuleType(package + ".step")
    steps.__path__ = [str(root / "core" / "step")]
    sys.modules[package], sys.modules[package + ".step"] = namespace, steps
    c = contact_case
    try:
        config_module = importlib.import_module(package + ".config")
        split_module = importlib.import_module(package + ".step.split")
        model = importlib.import_module(package + ".model")
        split_config = config_module.SplitConfig(
            {
                "max_length": 150,
                "char_list": ["。", "！", "\\n"],
                "max_count": 2,
                "per_char_delay": 0,
                "delay_scope_str": "0~0",
                "show_typing": False,
                "tail_punc": [],
            }
        )
        step = split_module.SplitStep(
            SimpleNamespace(split=split_config, context=c.context)
        )

        async def hook(event, kind):
            if kind == EventType.OnDecoratingResultEvent:
                result = event.get_result()
                await step.handle(
                    model.OutContext(
                        event=event,
                        chain=result.chain,
                        is_llm=True,
                        plain=result.get_plain_text(),
                        gid=event.get_group_id(),
                        uid=event.get_sender_id(),
                        bid=event.get_self_id(),
                        group=model.StateManager.get_group(event.get_group_id()),
                        timestamp=event.message_obj.timestamp,
                    )
                )

        c.runtime.hook = hook
        event = c.cron()
        wrapper = await c.begin(event)
        result = await SendMessageToUserTool().call(
            wrapper, messages=[{"type": "plain", "text": "第一段。第二段！第三段"}]
        )
        assert result.startswith("Message sent")
        assert c.actual_send.await_count == 2
        assert [
            [part.text for part in call.args[1].chain if isinstance(part, Plain)]
            for call in c.actual_send.call_args_list
        ] == [["第一段。"], ["第二段！", "第三段"]]
        assert (await c.runtime.state.facts(c.session))["unanswered_contacts"] == 1
    finally:
        await c.runtime.close()
        for name in list(sys.modules):
            if name == package or name.startswith(package + "."):
                sys.modules.pop(name, None)
