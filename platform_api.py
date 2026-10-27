"""通过已连接的 AstrBot OneBot 适配器读取目标近期消息。"""

import asyncio
from typing import Any

from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType


def physical_chat_id(session: MessageSession, platform: Any) -> str:
    """取得平台 API 使用的真实群号或用户号。

    Args:
        session: 完整 AstrBot 会话。
        platform: 对应的平台实例。

    Returns:
        平台侧聊天标识，不改变原会话的上下文作用域。
    """
    if (
        platform.meta().name == "aiocqhttp"
        and session.message_type == MessageType.GROUP_MESSAGE
    ):
        return session.session_id.rsplit("_", 1)[-1]
    return session.session_id


class OneBotClient:
    """复用适配器鉴权和连接，每次读取目标近期消息。"""

    def __init__(self, timeout: float = 8):
        self.timeout = timeout

    async def call(self, platform: Any, action: str, **params: Any) -> Any:
        """调用只读 OneBot API 并检查失败响应。

        Args:
            platform: AstrBot aiocqhttp 适配器。
            action: 要调用的 API 名称。
            **params: API 参数。

        Returns:
            API 数据，兼容已解包及完整响应。

        Raises:
            ValueError: 平台不支持或 API 返回失败。
            TimeoutError: 平台在限制时间内未返回。
        """
        if platform.meta().name != "aiocqhttp":
            raise ValueError("此平台没有接入 OneBot 历史读取能力。")
        result = await asyncio.wait_for(
            platform.bot.call_action(action, **params), timeout=self.timeout
        )
        if isinstance(result, dict) and ("retcode" in result or "status" in result):
            if result.get("status") == "failed" or result.get("retcode", 0) != 0:
                raise ValueError(
                    f"{action} 失败：{result.get('message') or result.get('wording') or result.get('retcode')}"
                )
            return result.get("data")
        return result
