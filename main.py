"""会话查询与未来任务增强插件入口。"""

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register

from .activity import GroupActivity
from .contact_delivery import ContactRuntime
from .jev import JevJudge
from .participation import ParticipationGate
from .platform_api import OneBotClient
from .private_proactive import PrivateProactive
from .private_status import format_time, render_status
from .sessions import require_admin
from .tasks import TaskService, proactive_enabled
from .tools import (
    FutureTaskPlusTool,
    GetChatContextTool,
    GetContactStateTool,
    GetGroupActivityTool,
    ListChatSessionsTool,
)


@register(
    "astrbot_plugin_krypton",
    "nagatoquin33",
    "氪：私聊日程启动、原生未来任务增强与可选 JEV 群聊参与判断",
    "0.5.0",
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
        self.contacts = ContactRuntime(
            context,
            self.plugin_config,
            self.get_kv_data,
            self.put_kv_data,
            self.private_proactive,
        )
        self.private_proactive.contact_state = self.contacts.state
        self.service = TaskService(context, contact_runtime=self.contacts)
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
                contact_runtime=self.contacts,
            ),
            GetContactStateTool(runtime=self.contacts),
            GetGroupActivityTool(activity=self.activity),
            FutureTaskPlusTool(service=self.service),
        )

    async def initialize(self):
        await self.contacts.initialize()
        if getattr(self.context, "cron_manager", None) is not None and hasattr(
            self.context.cron_manager, "ctx"
        ):
            await self.private_proactive.start()

    @filter.on_astrbot_loaded()
    async def restore_private_proactive(self):
        await self.contacts.initialize()
        await self.private_proactive.start()

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=-100)
    async def observe_private_message(self, event):
        """普通私聊即可启动沉默等待，不要求用户先提出创建任务。"""
        try:
            await self.contacts.observe(event)
            await self.private_proactive.on_message(event)
        except Exception:
            logger.exception("私聊自主行动启动失败")

    @filter.command("氪状态", alias={"krypton_status"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def private_proactive_status(self, event):
        """查看私聊自动启动的等待、跳过原因与最近执行结果，仅管理员可用。"""
        require_admin(event)
        event.set_extra("krypton_private_diagnostic", True)
        result = await self.private_proactive.get_status()
        yield event.plain_result(render_status(result))

    @filter.command("氪重置", alias={"krypton_reset"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def reset_private_proactive(self, event, qq: str = ""):
        """重新开始一个已配置 QQ 的首次唤醒等待，仅管理员可用。"""
        require_admin(event)
        event.set_extra("krypton_private_diagnostic", True)
        try:
            result = await self.private_proactive.reset_wait(qq)
            text = f"已重新等待 QQ {result['qq']}，预计唤醒：" + format_time(
                result["deadline"], result["timezone"]
            )
        except ValueError as exc:
            text = str(exc)
        yield event.plain_result(text)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=-100)
    async def judge_group_participation(self, event):
        """通过可选 JEV 筛选普通群消息，复用原生主 Agent。"""
        await self.contacts.observe(event)
        if await self.participation.consider(event):
            event.is_at_or_wake_command = True
            event.is_wake = True

    @filter.after_message_sent()
    async def remember_reply(self, event):
        """记录实际回复，更新接话冷却和私聊沉默时间。"""
        if event.get_extra("krypton_private_diagnostic", False):
            return
        self.participation.note_sent(event)
        if event.get_extra("krypton_contact_delivery", False):
            return
        try:
            await self.private_proactive.on_reply(event)
        except Exception:
            logger.exception("私聊沉默时间更新失败")

    async def terminate(self):
        await self.contacts.close()
        await self.private_proactive.close()
        await self.participation.close()

    @filter.on_agent_begin()
    async def begin_contact(self, event, run_context):
        """只为主动联系任务安装本轮发送适配，不修改全局工具。"""
        await self.contacts.begin(event, run_context)

    @filter.on_llm_tool_respond()
    async def record_contact_tool_result(self, event, tool, tool_args, tool_result):
        if tool.name == "send_message_to_user":
            self.contacts.reconcile_history(event)

    @filter.on_agent_done()
    async def finish_contact(self, event, run_context, response):
        await self.contacts.finish(event)

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
