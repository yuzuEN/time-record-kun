"""Discord 語音頻道時間記錄機器人。"""
import csv
import io
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional, Union
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from db import Database
from stats import (
    aggregate, aggregate_by_channel, aggregate_by_user, day_start_ts, fmt_date, fmt_duration, parse_range,
)

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Taipei"))
GUILD_ID = os.getenv("GUILD_ID")
DB_PATH = os.getenv("DB_PATH", "timerecord.db")
HEARTBEAT_SECONDS = 60

# 類別代碼 -> (emoji, 顯示名稱)
CATEGORIES = {
    "study": ("📚", "讀書"),
    "rest": ("☕", "休息"),
    "work": ("💼", "工作"),
    "minecraft": ("⛏️", "Minecraft"),
}
# 每日排行榜只計算這些類別的時數
REPORT_CATEGORIES = ("study", "work")
CATEGORY_CHOICES =[app_commands.Choice(name=f"{e} {n}", value=k) for k, (e, n) in CATEGORIES.items()]

log = logging.getLogger("timerecord")


def cat_label(category: str) -> str:
    emoji, name = CATEGORIES.get(category, ("❔", category))
    return f"{emoji} {name}"


def fmt_clock(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M:%S")


def local_now() -> datetime:
    return datetime.now(TZ)


def parse_minute(text: str) -> Optional[int]:
    """把 "HH:MM" 轉成午夜後第幾分鐘；格式不對回傳 None。"""
    try:
        t = datetime.strptime(text.strip(), "%H:%M")
    except ValueError:
        return None
    return t.hour * 60 + t.minute


def fmt_minute(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def build_daily_report(db: Database, guild_id: int, day, now: int) -> discord.Embed:
    """某一天全伺服器的排行榜：只計算 REPORT_CATEGORIES 的時數，依總時數排序，列出每個人各類別各待多久。"""
    start = day_start_ts(day, TZ)
    end = min(day_start_ts(day + timedelta(days=1), TZ), now)
    by_user = aggregate_by_user(db.query_sessions(guild_id, None, start, end), start, end, now)
    counted = {uid: {cat: totals.get(cat, 0) for cat in REPORT_CATEGORIES} for uid, totals in by_user.items()}
    ranking = sorted(
        ((uid, totals, sum(totals.values())) for uid, totals in counted.items() if sum(totals.values())),
        key=lambda x: x[2],
        reverse=True,
    )

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, totals, total) in enumerate(ranking):
        parts = [f"{CATEGORIES[cat][0]} {fmt_duration(totals[cat])}" for cat in REPORT_CATEGORIES if totals[cat]]
        line = f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{uid}>　**{fmt_duration(total)}**\n　" + "　".join(parts)
        if sum(len(l) + 1 for l in lines) + len(line) > 3900:
            lines.append(f"…還有 {len(ranking) - i} 人")
            break
        lines.append(line)

    embed = discord.Embed(
        title=f"🏆 {fmt_date(day)} 每日排行榜",
        description="\n".join(lines) if lines else "這天沒有任何人進入讀書或工作頻道",
        color=0xF1C40F,
    )
    grand = {cat: sum(t[cat] for _, t, _ in ranking) for cat in REPORT_CATEGORIES}
    for cat in REPORT_CATEGORIES:
        embed.add_field(name=cat_label(cat), value=fmt_duration(grand[cat]), inline=True)
    embed.set_footer(text=f"共 {len(ranking)} 人・時區 {TZ.key}")
    return embed


class TimeRecordBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()  # 已包含 guilds 與 voice_states，不需要特權 intent
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.db = Database(DB_PATH)
        # 在心跳開始覆寫之前，先記下上次關機前最後存活的時間
        last_alive = self.db.get_meta("last_alive")
        self.prev_alive = int(last_alive) if last_alive else None
        self.reconciled = False

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.heartbeat.start()

    @tasks.loop(seconds=HEARTBEAT_SECONDS)
    async def heartbeat(self):
        self.db.set_meta("last_alive", str(int(time.time())))
        if self.is_ready():
            try:
                await self.post_daily_reports()
            except Exception:
                log.exception("每日排行榜發送失敗")  # 不讓例外中斷心跳

    async def post_daily_reports(self):
        """每天到了設定的時間，就把前一天的排行榜貼到設定的頻道。機器人在那時離線的話，會在下次啟動時補發。"""
        now_dt = local_now()
        today = now_dt.date()
        minute = now_dt.hour * 60 + now_dt.minute
        now = int(time.time())
        for row in self.db.get_report_settings():
            if row["report_last"] and row["report_last"] >= today.isoformat():
                continue
            if minute < row["report_time"]:
                continue
            # 先標記為已處理，失敗也不每分鐘重試，避免權限錯誤時不斷洗 log
            self.db.set_report_last(row["guild_id"], today.isoformat())
            channel = self.get_channel(row["report_channel_id"])
            if channel is None:
                log.warning("找不到每日排行榜頻道 %s（伺服器 %s）", row["report_channel_id"], row["guild_id"])
                continue
            embed = build_daily_report(self.db, row["guild_id"], today - timedelta(days=1), now)
            try:
                if isinstance(channel, discord.ForumChannel):
                    # 論壇頻道：每天開一篇貼文，大家可以在底下留言
                    await channel.create_thread(
                        name=embed.title, embed=embed, allowed_mentions=discord.AllowedMentions.none()
                    )
                else:
                    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.exception("無法在頻道 %s 發送每日排行榜", channel.id)

    async def on_ready(self):
        log.info("已登入為 %s (id=%s)", self.user, self.user.id)
        if not self.reconciled:
            self.reconciled = True
            self.reconcile()

    def reconcile(self):
        """啟動時校正：機器人離線期間可能漏掉的進出事件。"""
        now = int(time.time())
        cutoff = min(self.prev_alive or now, now)

        # 1) 未結束的時段：若使用者已不在該頻道，就以最後存活時間結束
        for s in self.db.get_open_sessions():
            guild = self.get_guild(s["guild_id"])
            member = guild.get_member(s["user_id"]) if guild else None
            voice_channel = member.voice.channel if member and member.voice else None
            if voice_channel is None or voice_channel.id != s["channel_id"]:
                self.db.close_session(s["id"], cutoff)

        # 2) 目前已在追蹤頻道內但沒有時段的人，從現在開始記錄
        for guild in self.guilds:
            for channel_id, category in self.db.list_channels(guild.id):
                channel = guild.get_channel(channel_id)
                if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                    continue
                for m in channel.members:
                    if not m.bot and self.db.get_open_session(guild.id, m.id) is None:
                        self.db.open_session(guild.id, m.id, channel.id, category, now)
        log.info("啟動校正完成")

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot or before.channel == after.channel:
            return  # 靜音、開關鏡頭等狀態變化不算進出
        now = int(time.time())
        guild_id = member.guild.id
        if before.channel is not None:
            self.db.close_user_sessions(guild_id, member.id, now)
        if after.channel is not None:
            category = self.db.get_category(guild_id, after.channel.id)
            if category:
                self.db.open_session(guild_id, member.id, after.channel.id, category, now)


bot = TimeRecordBot()


# ================= 管理指令 =================

@bot.tree.command(name="setup", description="把語音頻道設定為某個類別（讀書/休息/工作/Minecraft）")
@app_commands.describe(channel="要追蹤的語音頻道", category="頻道類別")
@app_commands.choices(category=CATEGORY_CHOICES)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def setup_cmd(interaction: discord.Interaction, channel: discord.VoiceChannel, category: app_commands.Choice[str]):
    bot.db.set_channel(interaction.guild_id, channel.id, category.value)
    # 頻道裡已經有人的話，以新類別重新開始計時
    now = int(time.time())
    for m in channel.members:
        if not m.bot:
            bot.db.close_user_sessions(interaction.guild_id, m.id, now)
            bot.db.open_session(interaction.guild_id, m.id, channel.id, category.value, now)
    await interaction.response.send_message(f"✅ 已將 {channel.mention} 設定為 **{cat_label(category.value)}**")


@bot.tree.command(name="unset", description="停止追蹤某個語音頻道")
@app_commands.describe(channel="要取消追蹤的語音頻道")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def unset_cmd(interaction: discord.Interaction, channel: discord.VoiceChannel):
    if bot.db.unset_channel(interaction.guild_id, channel.id):
        bot.db.close_channel_sessions(interaction.guild_id, channel.id, int(time.time()))
        await interaction.response.send_message(f"🗑️ 已停止追蹤 {channel.mention}")
    else:
        await interaction.response.send_message(f"{channel.mention} 本來就沒有被追蹤", ephemeral=True)


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, guild_id: int, target: discord.Member, start: int, end: int):
        super().__init__(timeout=60)
        self.guild_id, self.target, self.start, self.end = guild_id, target, start, end
        self.interaction: Optional[discord.Interaction] = None  # 送出確認訊息的那次互動，逾時時用來編輯訊息

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.interaction:
            try:
                await self.interaction.edit_original_response(content="⌛ 已逾時，沒有刪除任何紀錄。請重新執行 `/delete`。", view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="確認刪除", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        count = bot.db.delete_range(self.guild_id, self.target.id, self.start, self.end)
        log.info(
            "%s 刪除了 %s 在 %s ~ %s 的紀錄（%d 個時段）",
            interaction.user, self.target, self.start, self.end, count,
        )
        await interaction.response.edit_message(content=f"🗑️ 已刪除，共影響 {count} 個時段。", view=None)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="已取消，沒有刪除任何紀錄。", view=None)


@bot.tree.command(name="delete", description="刪除某成員在指定時間段的紀錄（例如忘記退出語音頻道）")
@app_commands.describe(
    member="要修正紀錄的成員",
    date="開始日期，格式 YYYY-MM-DD",
    start="開始時間，格式 HH:MM 或 HH:MM:SS",
    end="結束時間，格式 HH:MM 或 HH:MM:SS（比開始時間早則視為隔天）",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def delete_cmd(interaction: discord.Interaction, member: discord.Member, date: str, start: str, end: str):
    try:
        start_ts, end_ts = parse_range(date, start, end, TZ)
    except ValueError:
        await interaction.response.send_message("格式錯誤，或開始與結束時間相同。日期用 `YYYY-MM-DD`，時間用 `HH:MM` 或 `HH:MM:SS`，例如 `2026-10-04` `21:50:08` `21:51`", ephemeral=True)
        return

    now = int(time.time())
    end_ts = min(end_ts, now)
    if start_ts >= end_ts:
        await interaction.response.send_message("開始時間必須早於現在", ephemeral=True)
        return

    sessions = bot.db.query_sessions(interaction.guild_id, member.id, start_ts, end_ts)
    removed = sum(aggregate_by_user(sessions, start_ts, end_ts, now).get(member.id, {}).values())
    if not removed:
        await interaction.response.send_message("這段時間沒有任何紀錄", ephemeral=True)
        return

    range_txt = f"{datetime.fromtimestamp(start_ts, TZ):%m/%d %H:%M:%S} → {datetime.fromtimestamp(end_ts, TZ):%m/%d %H:%M:%S}"
    view = ConfirmDeleteView(interaction.guild_id, member, start_ts, end_ts)
    view.interaction = interaction
    await interaction.response.send_message(
        f"⚠️ 即將刪除 **{member.display_name}** 在 `{range_txt}` 的紀錄，"
        f"共 **{fmt_duration(removed)}**（{len(sessions)} 個時段）。此動作無法復原，確定嗎？",
        view=view,
        ephemeral=True,
    )


@bot.tree.command(name="channels", description="列出目前追蹤中的語音頻道")
@app_commands.guild_only()
async def channels_cmd(interaction: discord.Interaction):
    rows = bot.db.list_channels(interaction.guild_id)
    if not rows:
        await interaction.response.send_message("目前沒有追蹤任何頻道，請管理員使用 `/setup` 設定。", ephemeral=True)
        return
    lines = [f"{cat_label(cat)} → <#{cid}>" for cid, cat in rows]
    embed = discord.Embed(title="🎧 追蹤中的語音頻道", description="\n".join(lines), color=0x5865F2)
    await interaction.response.send_message(embed=embed)


PRIVACY_LABELS = {
    "public": "🌐 公開：所有人都能查詢所有人的時間",
    "private": "🔒 私人：只能查詢自己的時間（管理員可以查詢所有人）",
}


@bot.tree.command(name="privacy", description="設定成員能不能查詢別人的時間（不填則顯示目前設定）")
@app_commands.describe(mode="查詢權限")
@app_commands.choices(mode=[app_commands.Choice(name=label, value=key) for key, label in PRIVACY_LABELS.items()])
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def privacy_cmd(interaction: discord.Interaction, mode: Optional[app_commands.Choice[str]] = None):
    if mode is None:
        current = bot.db.get_privacy(interaction.guild_id)
        await interaction.response.send_message(f"目前的查詢權限：{PRIVACY_LABELS[current]}", ephemeral=True)
        return
    bot.db.set_privacy(interaction.guild_id, mode.value)
    await interaction.response.send_message(f"✅ 查詢權限已設定為 {PRIVACY_LABELS[mode.value]}")


report_group = app_commands.Group(
    name="report",
    description="每天在指定時間自動公告前一天的排行榜",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@report_group.command(name="set", description="設定每日排行榜要公告在哪個頻道、每天幾點公告")
@app_commands.rename(at="time")
@app_commands.describe(
    channel="公告用的文字、公告或論壇頻道（論壇頻道會每天開一篇新貼文）",
    at="每天公告的時間，格式 HH:MM（預設 00:00）",
)
async def report_set_cmd(
    interaction: discord.Interaction,
    channel: Union[discord.TextChannel, discord.ForumChannel],
    at: str = "00:00",
):
    minute = parse_minute(at)
    if minute is None:
        await interaction.response.send_message("時間格式錯誤，請用 HH:MM，例如 `08:00` 或 `23:30`。", ephemeral=True)
        return
    is_forum = isinstance(channel, discord.ForumChannel)
    if is_forum and channel.is_media():
        await interaction.response.send_message("不支援媒體頻道，請改用文字、公告或論壇頻道。", ephemeral=True)
        return
    if is_forum and channel.flags.require_tag:
        await interaction.response.send_message(
            f"{channel.mention} 要求發文必須選標籤，機器人無法發文，請先在頻道設定關閉「發文時需要標籤」。", ephemeral=True
        )
        return
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        action = "發佈貼文" if is_forum else "發送訊息"
        await interaction.response.send_message(
            f"機器人在 {channel.mention} 沒有「檢視頻道」「{action}」「嵌入連結」權限，請先調整頻道權限。", ephemeral=True
        )
        return
    # 今天的公告時間還沒到就從今天開始公告，已經過了就從明天開始，避免設定後立刻補發
    now_dt = local_now()
    started = now_dt.hour * 60 + now_dt.minute >= minute
    last = now_dt.date() if started else now_dt.date() - timedelta(days=1)
    bot.db.set_report_channel(interaction.guild_id, channel.id, last.isoformat(), minute)
    first = "明天" if started else "今天"
    await interaction.response.send_message(
        f"✅ 每天 {fmt_minute(minute)} 會在 {channel.mention} 公告前一天的排行榜（第一次是{first} {fmt_minute(minute)}）"
    )


@report_group.command(name="off", description="關閉每日排行榜公告")
async def report_off_cmd(interaction: discord.Interaction):
    bot.db.set_report_channel(interaction.guild_id, None)
    await interaction.response.send_message("🔕 已關閉每日排行榜公告")


@report_group.command(name="preview", description="預覽今天到目前為止的每日排行榜（只有自己看得到）")
async def report_preview_cmd(interaction: discord.Interaction):
    embed = build_daily_report(bot.db, interaction.guild_id, datetime.now(TZ).date(), int(time.time()))
    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.tree.add_command(report_group)


# ================= 統計指令 =================

async def ensure_can_view(interaction: discord.Interaction, target: discord.abc.User) -> bool:
    """依伺服器的查詢權限設定，檢查能不能查詢 target 的紀錄；不行時回覆錯誤訊息。"""
    if (
        target.id == interaction.user.id
        or bot.db.get_privacy(interaction.guild_id) == "public"
        or interaction.user.guild_permissions.manage_guild
    ):
        return True
    await interaction.response.send_message("🔒 這個伺服器設定為只能查詢自己的紀錄（管理員除外）", ephemeral=True)
    return False


@bot.tree.command(name="today", description="查看今天在各類頻道待了多久")
@app_commands.describe(member="要查詢的成員（預設為自己）")
@app_commands.guild_only()
async def today_cmd(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    target = member or interaction.user
    if not await ensure_can_view(interaction, target):
        return
    now = int(time.time())
    today = datetime.now(TZ).date()
    start = day_start_ts(today, TZ)
    sessions = bot.db.query_sessions(interaction.guild_id, target.id, start, now)
    totals = aggregate(sessions, start, now, now, TZ).get(today, {})

    embed = discord.Embed(title=f"📅 {fmt_date(today)} 今日統計", color=0x57F287)
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    for cat in CATEGORIES:
        embed.add_field(name=cat_label(cat), value=fmt_duration(totals.get(cat, 0)), inline=True)
    embed.add_field(name="⏱️ 合計", value=fmt_duration(sum(totals.values())), inline=False)

    current = bot.db.get_open_session(interaction.guild_id, target.id)
    if current:
        embed.set_footer(text=f"目前在 {cat_label(current['category'])} 頻道，已待 {fmt_duration(now - current['join_ts'])}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="stats", description="查看最近 N 天每天的統計")
@app_commands.describe(days="天數（1~186，預設 7；超過 31 天改為每週一行）", member="要查詢的成員（預設為自己）")
@app_commands.guild_only()
async def stats_cmd(interaction: discord.Interaction, days: app_commands.Range[int, 1, 186] = 7, member: Optional[discord.Member] = None):
    target = member or interaction.user
    if not await ensure_can_view(interaction, target):
        return
    now = int(time.time())
    today = datetime.now(TZ).date()
    first_day = today - timedelta(days=days - 1)
    start = day_start_ts(first_day, TZ)
    sessions = bot.db.query_sessions(interaction.guild_id, target.id, start, now)
    by_day = aggregate(sessions, start, now, now, TZ)

    # 天數太多時一天一行會超過 embed 字數上限，改成以週（週一～週日）為單位
    weekly = days > 31
    rows = {}  # 標籤 -> {類別: 秒數}
    grand = {cat: 0 for cat in CATEGORIES}
    for i in range(days):
        d = first_day + timedelta(days=i)
        if weekly:
            week_start = max(d - timedelta(days=d.weekday()), first_day)
            week_end = min(week_start + timedelta(days=6 - week_start.weekday()), today)
            label = f"{week_start:%m/%d}~{week_end:%m/%d}"
        else:
            label = fmt_date(d)
        row = rows.setdefault(label, {cat: 0 for cat in CATEGORIES})
        for cat, sec in by_day.get(d, {}).items():
            if cat in row:
                row[cat] += sec
                grand[cat] += sec

    lines = []
    for label, row in rows.items():
        parts = [f"{CATEGORIES[cat][0]} {fmt_duration(sec)}" for cat, sec in row.items() if sec]
        lines.append(f"`{label}` " + ("　".join(parts) if parts else "—"))

    embed = discord.Embed(title=f"📊 最近 {days} 天統計", description="\n".join(lines), color=0xFEE75C)
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    for cat in CATEGORIES:
        embed.add_field(
            name=cat_label(cat),
            value=f"{fmt_duration(grand[cat])}\n日均 {fmt_duration(grand[cat] // days)}",
            inline=True,
        )
    embed.add_field(name="⏱️ 合計", value=fmt_duration(sum(grand.values())), inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="log", description="查看某天的進出時間紀錄")
@app_commands.describe(date="日期，格式 YYYY-MM-DD（預設今天）", member="要查詢的成員（預設為自己）")
@app_commands.guild_only()
async def log_cmd(interaction: discord.Interaction, date: Optional[str] = None, member: Optional[discord.Member] = None):
    target = member or interaction.user
    if not await ensure_can_view(interaction, target):
        return
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date() if date else datetime.now(TZ).date()
    except ValueError:
        await interaction.response.send_message("日期格式錯誤，請使用 `YYYY-MM-DD`，例如 `2026-09-29`", ephemeral=True)
        return

    now = int(time.time())
    start = day_start_ts(day, TZ)
    end = day_start_ts(day + timedelta(days=1), TZ)
    sessions = bot.db.query_sessions(interaction.guild_id, target.id, start, end)

    lines = []
    for s in sessions:
        leave = s["leave_ts"]
        # 跨日的時段在前後加上日期標示
        join_txt = fmt_clock(s["join_ts"]) if s["join_ts"] >= start else f"(前一天) {fmt_clock(s['join_ts'])}"
        if leave is None:
            leave_txt = "進行中"
        elif leave > end:
            leave_txt = f"(隔天) {fmt_clock(leave)}"
        else:
            leave_txt = fmt_clock(leave)
        duration = fmt_duration((leave if leave is not None else now) - s["join_ts"])
        emoji = CATEGORIES.get(s["category"], ("❔",))[0]
        lines.append(f"{emoji} <#{s['channel_id']}>　`{join_txt} → {leave_txt}`　({duration})")

    description = "\n".join(lines) if lines else "這天沒有任何紀錄"
    if len(description) > 4000:
        description = description[:4000] + "\n…（紀錄過多，已截斷，可用 /export 匯出完整資料）"
    embed = discord.Embed(title=f"🕒 {fmt_date(day)} 進出紀錄", description=description, color=0xEB459E)
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="channeltime", description="查看某天在每個語音頻道各待了多久")
@app_commands.describe(date="日期，格式 YYYY-MM-DD（預設今天）", member="要查詢的成員（預設為自己）")
@app_commands.guild_only()
async def channeltime_cmd(interaction: discord.Interaction, date: Optional[str] = None, member: Optional[discord.Member] = None):
    target = member or interaction.user
    if not await ensure_can_view(interaction, target):
        return
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date() if date else datetime.now(TZ).date()
    except ValueError:
        await interaction.response.send_message("日期格式錯誤，請使用 `YYYY-MM-DD`，例如 `2026-09-29`", ephemeral=True)
        return

    now = int(time.time())
    start = day_start_ts(day, TZ)
    end = min(day_start_ts(day + timedelta(days=1), TZ), now)
    sessions = bot.db.query_sessions(interaction.guild_id, target.id, start, end)
    by_channel = aggregate_by_channel(sessions, start, end, now)

    rows = sorted(
        ((cid, cat, sec) for cid, totals in by_channel.items() for cat, sec in totals.items()),
        key=lambda x: x[2],
        reverse=True,
    )
    lines = [f"{CATEGORIES.get(cat, ('❔',))[0]} <#{cid}>　**{fmt_duration(sec)}**" for cid, cat, sec in rows]
    if lines:
        lines.append(f"\n⏱️ 合計 **{fmt_duration(sum(sec for _, _, sec in rows))}**")
    embed = discord.Embed(
        title=f"🎧 {fmt_date(day)} 各頻道時間",
        description="\n".join(lines) if lines else "這天沒有任何紀錄",
        color=0x3BA55D,
    )
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="status", description="查看目前在哪個追蹤頻道、待了多久")
@app_commands.describe(member="要查詢的成員（預設為自己）")
@app_commands.guild_only()
async def status_cmd(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    target = member or interaction.user
    if not await ensure_can_view(interaction, target):
        return
    current = bot.db.get_open_session(interaction.guild_id, target.id)
    if current is None:
        await interaction.response.send_message(f"**{target.display_name}** 目前不在任何追蹤中的語音頻道")
        return
    elapsed = int(time.time()) - current["join_ts"]
    await interaction.response.send_message(
        f"**{target.display_name}** 從 `{fmt_clock(current['join_ts'])}` 起在 <#{current['channel_id']}>"
        f"（{cat_label(current['category'])}），已待 **{fmt_duration(elapsed)}**"
    )


@bot.tree.command(name="leaderboard", description="伺服器排行榜")
@app_commands.describe(category="依哪個類別排名（預設讀書）", days="統計最近幾天（1~365，預設 7）")
@app_commands.choices(category=CATEGORY_CHOICES)
@app_commands.guild_only()
async def leaderboard_cmd(
    interaction: discord.Interaction,
    category: Optional[app_commands.Choice[str]] = None,
    days: app_commands.Range[int, 1, 365] = 7,
):
    cat = category.value if category else "study"
    now = int(time.time())
    start = day_start_ts(datetime.now(TZ).date() - timedelta(days=days - 1), TZ)
    sessions = bot.db.query_sessions(interaction.guild_id, None, start, now)
    by_user = aggregate_by_user(sessions, start, now, now)

    ranking = sorted(
        ((uid, totals.get(cat, 0)) for uid, totals in by_user.items() if totals.get(cat, 0) > 0),
        key=lambda x: x[1],
        reverse=True,
    )[:10]
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{uid}>　**{fmt_duration(sec)}**"
        for i, (uid, sec) in enumerate(ranking)
    ]
    embed = discord.Embed(
        title=f"🏆 {cat_label(cat)} 排行榜（最近 {days} 天）",
        description="\n".join(lines) if lines else "還沒有任何紀錄",
        color=0xF1C40F,
    )
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="export", description="匯出最近 N 天的原始進出紀錄（CSV）")
@app_commands.describe(days="天數（1~365，預設 30）", member="要匯出的成員（預設為自己）")
@app_commands.guild_only()
async def export_cmd(interaction: discord.Interaction, days: app_commands.Range[int, 1, 365] = 30, member: Optional[discord.Member] = None):
    target = member or interaction.user
    if not await ensure_can_view(interaction, target):
        return
    now = int(time.time())
    start = day_start_ts(datetime.now(TZ).date() - timedelta(days=days - 1), TZ)
    sessions = bot.db.query_sessions(interaction.guild_id, target.id, start, now)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["category", "channel_id", "join_time", "leave_time", "duration_minutes"])
    for s in sessions:
        leave = s["leave_ts"]
        writer.writerow([
            s["category"],
            s["channel_id"],
            datetime.fromtimestamp(s["join_ts"], TZ).isoformat(timespec="seconds"),
            datetime.fromtimestamp(leave, TZ).isoformat(timespec="seconds") if leave is not None else "",
            round(((leave if leave is not None else now) - s["join_ts"]) / 60, 1),
        ])
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))  # 加 BOM 讓 Excel 正確顯示中文
    filename = f"timerecord_{target.id}_{datetime.now(TZ):%Y%m%d}.csv"
    await interaction.response.send_message(
        f"📁 {target.display_name} 最近 {days} 天的紀錄（共 {len(sessions)} 筆）",
        file=discord.File(data, filename=filename),
        ephemeral=True,
    )


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("請在 .env 設定 DISCORD_TOKEN")
    bot.run(TOKEN, log_level=logging.INFO)
