"""Discord 語音頻道時間記錄機器人。"""
import csv
import io
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from db import Database
from stats import aggregate, aggregate_by_user, day_start_ts, fmt_date, fmt_duration

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
CATEGORY_CHOICES = [app_commands.Choice(name=f"{e} {n}", value=k) for k, (e, n) in CATEGORIES.items()]

log = logging.getLogger("timerecord")


def cat_label(category: str) -> str:
    emoji, name = CATEGORIES.get(category, ("❔", category))
    return f"{emoji} {name}"


def fmt_clock(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M")


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


# ================= 統計指令 =================

@bot.tree.command(name="today", description="查看今天在各類頻道待了多久")
@app_commands.describe(member="要查詢的成員（預設為自己）")
@app_commands.guild_only()
async def today_cmd(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    target = member or interaction.user
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
@app_commands.describe(days="天數（1~31，預設 7）", member="要查詢的成員（預設為自己）")
@app_commands.guild_only()
async def stats_cmd(interaction: discord.Interaction, days: app_commands.Range[int, 1, 31] = 7, member: Optional[discord.Member] = None):
    target = member or interaction.user
    now = int(time.time())
    today = datetime.now(TZ).date()
    first_day = today - timedelta(days=days - 1)
    start = day_start_ts(first_day, TZ)
    sessions = bot.db.query_sessions(interaction.guild_id, target.id, start, now)
    by_day = aggregate(sessions, start, now, now, TZ)

    lines = []
    grand = {cat: 0 for cat in CATEGORIES}
    for i in range(days):
        d = first_day + timedelta(days=i)
        day_totals = by_day.get(d, {})
        parts = []
        for cat, (emoji, _) in CATEGORIES.items():
            sec = day_totals.get(cat, 0)
            grand[cat] += sec
            if sec:
                parts.append(f"{emoji} {fmt_duration(sec)}")
        lines.append(f"`{fmt_date(d)}` " + ("　".join(parts) if parts else "—"))

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


@bot.tree.command(name="status", description="查看目前在哪個追蹤頻道、待了多久")
@app_commands.describe(member="要查詢的成員（預設為自己）")
@app_commands.guild_only()
async def status_cmd(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    target = member or interaction.user
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
