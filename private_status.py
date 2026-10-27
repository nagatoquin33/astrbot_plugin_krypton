"""私聊启动状态的管理员命令展示。"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def format_time(timestamp, timezone):
    if timestamp is None:
        return "暂无"
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        tz = UTC
    return (
        datetime.fromtimestamp(timestamp, UTC)
        .astimezone(tz)
        .isoformat(timespec="seconds")
    )


def render_status(result):
    lines = [
        "氪 · 私聊启动状态",
        "功能：" + ("已开启" if result["enabled"] else "已关闭"),
        "启动器：" + ("已初始化" if result["started"] else "尚未初始化"),
    ]
    if not result["targets"]:
        lines.append(
            "未能匹配 QQ 与 OneBot 账号，请检查目标及平台 ID。"
            if result["has_qq_ids"]
            else "未填写目标 QQ。"
        )
    labels = {
        "uninitialized": "尚未建立等待，请检查配置并重载插件",
        "consumed": "本轮已处理，旧记录未保留详细结果",
        "existing_task": "本轮曾交给已有任务，当前未发现待执行任务",
        "running": "正在唤醒主 Agent",
        "completed": "本轮已完成，后续任务目前已不在待执行列表",
        "no_followup": "已唤醒，但未发现后续未来任务",
        "failed": "唤醒失败，本轮未自动重试",
        "cancelled": "本轮唤醒已取消",
    }
    for row in result["targets"]:
        lines.extend(["", "QQ：" + row["qq"]])
        if row["blocked"]:
            lines.append("未启动：" + row["blocked"])
        elif row["future"]:
            future = row["future"]
            lines.append(f"已有相关未来任务：{future['name']}（{future['id']}）")
            lines.append(
                "下次执行：" + format_time(future["next_run"], row["timezone"])
            )
        elif row["status"] == "waiting":
            if row["next_run"] is None:
                lines.append("等待项尚未注册，请重载插件检查日志。")
            else:
                lines.append(
                    "预计首次唤醒：" + format_time(row["next_run"], row["timezone"])
                )
        else:
            lines.append(labels.get(row["status"], row["status"]))
        if row["last_error"]:
            lines.append("最近错误类型：" + row["last_error"])
    lines.extend(
        [
            "",
            "首次唤醒负责建立日程，不保证立即发送私聊消息。",
            "重新测试可用 /氪重置 QQ；不会删除或改动已有未来任务。",
        ]
    )
    return "\n".join(lines)
