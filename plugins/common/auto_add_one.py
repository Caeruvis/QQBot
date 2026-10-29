from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache

from chinese_calendar import is_workday
from nonebot import logger

from collections import defaultdict
from copy import deepcopy

from nonebot import on_message
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageSegment,
)

from utils.recall_map import add
from config import config


repeat = on_message(priority=5, block=False)

ALLOW_GROUPS = config.qq.groups
BOT_ID = config.qq.bot_ids

last_msg = {}
last_user = defaultdict(int)
repeated = defaultdict(bool)

# 固定使用北京时间
BEIJING_TZ = timezone(timedelta(hours=8))

START_TIME = time(7, 45)
END_TIME = time(16, 40)

# 记录各群最近一次参与复读计数的日期
last_active_date = {}


@lru_cache(maxsize=32)
def calendar_is_workday(day: date) -> bool:
    try:
        return is_workday(day)
    except NotImplementedError:
        # 缺少该年份的数据时停用；同一天只记录一次日志
        logger.error(
            f"缺少 {day.year} 年节假日数据，自动复读已停用。"
            "请升级 chinesecalendar 并重启机器人。"
        )
        return False


def repeat_is_enabled(now: datetime) -> bool:
    return (
        START_TIME <= now.time() < END_TIME
        and calendar_is_workday(now.date())
    )


def clear_repeat_state(group_id: int) -> None:
    last_msg.pop(group_id, None)
    last_user.pop(group_id, None)
    repeated.pop(group_id, None)
    last_active_date.pop(group_id, None)


def build_repeat_message(event: GroupMessageEvent) -> Message:
    original = deepcopy(event.original_message)

    # 原始消息保留了适配器预处理前的引用、@ 和正文
    reply_segment = next(
        (segment for segment in original if segment.type == "reply"),
        None,
    )

    reply_id = None
    if reply_segment is not None:
        reply_id = int(reply_segment.data["id"])
    elif event.reply is not None:
        reply_id = event.reply.message_id

    # 重新构造引用段，避免直接复制接收段中的附加字段
    body = Message([
        segment
        for segment in original
        if segment.type != "reply"
    ])

    if reply_id is None:
        return body

    # 原始消息没有 @ 被引用者时，依据引用信息补上
    if event.reply is not None:
        author_id = event.reply.sender.user_id

        if author_id is not None:
            already_at_author = any(
                segment.type == "at"
                and str(segment.data.get("qq")) == str(author_id)
                for segment in body
            )

            if not already_at_author:
                body = Message(
                    MessageSegment.at(author_id)
                ) + " " + body

    return Message(MessageSegment.reply(reply_id)) + body


@repeat.handle()
async def _(event: GroupMessageEvent):
    group_id = event.group_id
    user_id = event.user_id

    if group_id not in ALLOW_GROUPS:
        return

    if user_id in BOT_ID:
        return

    if user_id == int(event.self_id):
        return

    now = datetime.now(BEIJING_TZ)

    # 休息日、工作时间之外：清空状态并跳过
    if not repeat_is_enabled(now):
        clear_repeat_state(group_id)
        return

    # 即使夜间没有收到消息，第二天也会重新计数
    today = now.date()
    if last_active_date.get(group_id) != today:
        clear_repeat_state(group_id)
        last_active_date[group_id] = today

    msg = build_repeat_message(event)

    # 跳过空消息和仅含空白字符的消息
    if not any(
        segment.type != "text"
        or segment.data.get("text", "").strip()
        for segment in msg
    ):
        return

    should_repeat = (
        last_msg.get(group_id) == msg
        and last_user[group_id] != user_id
        and not repeated[group_id]
    )

    if should_repeat:
        sent = await repeat.send(msg)
        add(event.message_id, sent["message_id"])
        repeated[group_id] = True

    elif last_msg.get(group_id) != msg:
        repeated[group_id] = False

    last_msg[group_id] = msg
    last_user[group_id] = user_id