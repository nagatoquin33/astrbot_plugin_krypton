"""会话查询与未来任务增强插件入口。"""

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register

from .activity import GroupActivity
from .jev import JevJudge
from .participation import ParticipationGate
from .platform_api import OneBotClient
from .private_proactive import PrivateProactive
from .tasks import TaskService, proactive_enabled
from .tools import (
    FutureTaskPlusTool,
    GetChatContextTool,
    GetGroupActivityTool,
    ListChatSessionsTool,
)


@register(
    "astrbot_plugin_krypton",
    "nagatoquin33",
    "氪：私聊日程启动、原生未来任务增强与可选 JEV 群聊参与判断",
    "0.4.1",
)
class KryptonPlugin(Star):
    """注册会话工具，并使未来任务扩展服从原生能力开关。"""

    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        self.plugin_config = config if config is not None else {}
        self.participation = ParticipationGate(context, self.plugin_config, JevJudge())
        self.private_proactive = PrivateProactive(
            context, self.plugin_config, self.get_kv_data, self.put_kv_data
        )
        self.service = TaskService(context)
        self.platform_client = OneBotClient()
        self.activity = GroupActivity(
            context, self.platform_client, self.get_kv_data, self.put_kv_data
        )
        self.context.add_llm_tools(
            ListChatSessionsTool(plugin_context=context),
            GetChatContextTool(
                plugin_context=context,
                platform_client=self.platform_client,
                activity=self.activity,
            ),
            GetGroupActivityTool(activity=self.activity),
            FutureTaskPlusTool(service=self.service),
        )

    async def initialize(self):
        if getattr(self.context, "cron_manager", None) is not None and hasattr(
            self.context.cron_manager, "ctx"
        ):
            await self.private_proactive.start()

    @filter.on_astrbot_loaded()
    async def restore_private_proactive(self):
        await self.private_proactive.start()

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=-100)
    async def observe_private_message(self, event):
        """普通私聊即可启动沉默等待，不要求用户先提出创建任务。"""
        try:
            await self.private_proactive.on_message(event)
        except Exception:
            logger.exception("私聊自主行动启动失败")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=-100)
    async def judge_group_participation(self, event):
        """通过可选 JEV 筛选普通群消息，复用原生主 Agent。"""
        if await self.participation.consider(event):
            event.is_at_or_wake_command = True
            event.is_wake = True

    @filter.after_message_sent()
    async def remember_reply(self, event):
        """记录实际回复，更新接话冷却和私聊沉默时间。"""
        self.participation.note_sent(event)
        try:
            await self.private_proactive.on_reply(event)
        except Exception:
            logger.exception("私聊沉默时间更新失败")

    async def terminate(self):
        await self.private_proactive.close()
        await self.participation.close()

    @filter.on_llm_request()
    async def prepare_request(self, event, req):
        """只调整任务工具入口，不修改提示词或会话消息。

        Args:
            event: 当前消息事件。
            req: AstrBot 已构建的模型请求。
        """
        if req.func_tool is None:
            return
        if not proactive_enabled(self.context, event):
            req.func_tool.remove_tool("future_task_plus")
            return
        future = req.func_tool.get_tool("future_task_plus")
        if future is not None and future.active:
            # 只从当前请求移除基础入口，不修改全局工具或核心状态。
            req.func_tool.remove_tool("future_task")
