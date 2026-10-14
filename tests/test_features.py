"""/channeltime、查詢權限（/privacy）與每日排行榜（/report）的測試。執行：python -m unittest discover tests"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# 必須在 import bot 之前設定，避免測試建立或寫入真正的資料庫檔案
os.environ["DB_PATH"] = ":memory:"
os.environ["TIMEZONE"] = "Asia/Taipei"

import discord  # noqa: E402
from discord import app_commands  # noqa: E402

import bot  # noqa: E402
from db import Database  # noqa: E402
from stats import aggregate_by_channel, day_start_ts  # noqa: E402

G = 1
ME, OTHER = 100, 200
CH_STUDY, CH_STUDY2, CH_REST = 11, 12, 13


def add(db, user, channel, category, join, leave):
    db.open_session(G, user, channel, category, join)
    if leave is not None:
        db.close_user_sessions(G, user, leave)


def make_member(uid, admin=False):
    m = MagicMock()
    m.id = uid
    m.display_name = f"user{uid}"
    m.display_avatar.url = "https://example.com/a.png"
    m.guild_permissions.manage_guild = admin
    return m


def make_interaction(user):
    it = MagicMock()
    it.guild_id = G
    it.user = user
    it.response.send_message = AsyncMock()
    return it


def sent(interaction):
    """回傳 send_message 的 (content, kwargs)。"""
    args, kwargs = interaction.response.send_message.call_args
    return (args[0] if args else kwargs.get("content")), kwargs


class AggregateByChannelTest(unittest.TestCase):
    def rows(self, *sessions):
        return [
            {"channel_id": c, "category": cat, "join_ts": j, "leave_ts": l, "user_id": ME}
            for c, cat, j, l in sessions
        ]

    def test_sums_per_channel(self):
        s = self.rows((1, "study", 0, 100), (2, "study", 100, 150), (1, "study", 200, 300))
        self.assertEqual(aggregate_by_channel(s, 0, 1000, 1000), {1: {"study": 200}, 2: {"study": 50}})

    def test_clips_to_range(self):
        s = self.rows((1, "study", 0, 100))
        self.assertEqual(aggregate_by_channel(s, 50, 80, 1000), {1: {"study": 30}})

    def test_open_session_counts_until_now(self):
        s = self.rows((1, "rest", 100, None))
        self.assertEqual(aggregate_by_channel(s, 0, 1000, 400), {1: {"rest": 300}})

    def test_recategorized_channel_keeps_both_categories(self):
        s = self.rows((1, "study", 0, 100), (1, "work", 100, 130))
        self.assertEqual(aggregate_by_channel(s, 0, 1000, 1000), {1: {"study": 100, "work": 30}})

    def test_sessions_outside_range_are_ignored(self):
        s = self.rows((1, "study", 0, 100))
        self.assertEqual(aggregate_by_channel(s, 100, 200, 1000), {})


class GuildSettingsDbTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def test_privacy_defaults_to_public(self):
        self.assertEqual(self.db.get_privacy(G), "public")

    def test_set_privacy_per_guild(self):
        self.db.set_privacy(G, "private")
        self.assertEqual(self.db.get_privacy(G), "private")
        self.assertEqual(self.db.get_privacy(G + 1), "public")
        self.db.set_privacy(G, "public")
        self.assertEqual(self.db.get_privacy(G), "public")

    def test_report_channel_set_and_off(self):
        self.assertEqual(self.db.get_report_settings(), [])
        self.db.set_report_channel(G, 555, "2026-10-13")
        rows = [tuple(r) for r in self.db.get_report_settings()]
        self.assertEqual(rows, [(G, 555, "2026-10-13", 0)])
        self.db.set_report_channel(G, None)
        self.assertEqual(self.db.get_report_settings(), [])

    def test_report_settings_do_not_reset_privacy(self):
        self.db.set_privacy(G, "private")
        self.db.set_report_channel(G, 555, "2026-10-13")
        self.db.set_report_channel(G, None)
        self.assertEqual(self.db.get_privacy(G), "private")

    def test_privacy_does_not_reset_report_channel(self):
        self.db.set_report_channel(G, 555, "2026-10-13")
        self.db.set_privacy(G, "private")
        self.assertEqual(self.db.get_report_settings()[0]["report_channel_id"], 555)

    def test_report_time_defaults_to_midnight_and_is_stored(self):
        self.db.set_report_channel(G, 555, "2026-10-13")
        self.assertEqual(self.db.get_report_settings()[0]["report_time"], 0)
        self.db.set_report_channel(G, 555, "2026-10-13", 8 * 60 + 30)
        self.assertEqual(self.db.get_report_settings()[0]["report_time"], 510)

    def test_migrates_old_schema(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, privacy TEXT NOT NULL DEFAULT 'public', "
                "report_channel_id INTEGER, report_last TEXT)"
            )
            conn.execute("INSERT INTO guild_settings VALUES (1, 'public', 555, '2026-10-13')")
            conn.commit()
            conn.close()
            db = Database(path)
            self.assertEqual(db.get_report_settings()[0]["report_time"], 0)
            db.conn.close()

    def test_set_report_last(self):
        self.db.set_report_channel(G, 555, "2026-10-13")
        self.db.set_report_last(G, "2026-10-14")
        self.assertEqual(self.db.get_report_settings()[0]["report_last"], "2026-10-14")

    def test_existing_database_gets_new_table(self):
        # 舊版資料庫沒有 guild_settings，開啟時要自動建立且保留原本資料
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE channel_map (guild_id INTEGER, channel_id INTEGER, category TEXT, "
                         "PRIMARY KEY (guild_id, channel_id))")
            conn.execute("INSERT INTO channel_map VALUES (1, 2, 'study')")
            conn.commit()
            conn.close()
            db = Database(path)
            self.assertEqual(db.get_privacy(1), "public")
            self.assertEqual(db.list_channels(1), [(2, "study")])
            db.conn.close()


class BotTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database(":memory:")
        patcher = patch.object(bot.bot, "db", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.today = datetime.now(bot.TZ).date()
        self.yesterday = self.today - timedelta(days=1)
        self.y0 = day_start_ts(self.yesterday, bot.TZ)  # 昨天 00:00

    def at(self, hour, minute=0):
        """把「現在」的當地時間固定在今天的 hour:minute。"""
        dt = datetime.combine(self.today, datetime.min.time(), bot.TZ).replace(hour=hour, minute=minute)
        p = patch.object(bot, "local_now", return_value=dt)
        p.start()
        self.addCleanup(p.stop)


class EnsureCanViewTest(BotTestCase):
    async def test_public_allows_viewing_others(self):
        it = make_interaction(make_member(ME))
        self.assertTrue(await bot.ensure_can_view(it, make_member(OTHER)))
        it.response.send_message.assert_not_called()

    async def test_private_allows_self(self):
        self.db.set_privacy(G, "private")
        it = make_interaction(make_member(ME))
        self.assertTrue(await bot.ensure_can_view(it, make_member(ME)))

    async def test_private_blocks_others_for_non_admin(self):
        self.db.set_privacy(G, "private")
        it = make_interaction(make_member(ME))
        self.assertFalse(await bot.ensure_can_view(it, make_member(OTHER)))
        _, kwargs = sent(it)
        self.assertTrue(kwargs["ephemeral"])

    async def test_private_allows_admin(self):
        self.db.set_privacy(G, "private")
        it = make_interaction(make_member(ME, admin=True))
        self.assertTrue(await bot.ensure_can_view(it, make_member(OTHER)))

    async def test_private_is_per_guild(self):
        self.db.set_privacy(G + 1, "private")
        it = make_interaction(make_member(ME))
        self.assertTrue(await bot.ensure_can_view(it, make_member(OTHER)))


class QueryCommandsPrivacyTest(BotTestCase):
    """所有能指定 member 的查詢指令，在私人模式下都不能查別人。"""
    COMMANDS = ["today_cmd", "stats_cmd", "log_cmd", "channeltime_cmd", "status_cmd", "export_cmd"]

    async def test_non_admin_blocked_from_others(self):
        self.db.set_privacy(G, "private")
        add(self.db, OTHER, CH_STUDY, "study", self.y0, self.y0 + 100)
        for name in self.COMMANDS:
            with self.subTest(name):
                it = make_interaction(make_member(ME))
                await getattr(bot, name).callback(it, member=make_member(OTHER))
                it.response.send_message.assert_called_once()
                content, kwargs = sent(it)
                self.assertIn("只能查詢自己", content)
                self.assertTrue(kwargs["ephemeral"])

    async def test_self_and_admin_allowed(self):
        self.db.set_privacy(G, "private")
        for name in self.COMMANDS:
            for user, target in [(make_member(ME), None), (make_member(ME, admin=True), make_member(OTHER))]:
                with self.subTest(name, admin=user.guild_permissions.manage_guild):
                    it = make_interaction(user)
                    await getattr(bot, name).callback(it, member=target)
                    content, _ = sent(it)
                    self.assertNotIn("只能查詢自己", content or "")

    async def test_public_allows_others(self):
        for name in self.COMMANDS:
            with self.subTest(name):
                it = make_interaction(make_member(ME))
                await getattr(bot, name).callback(it, member=make_member(OTHER))
                content, _ = sent(it)
                self.assertNotIn("只能查詢自己", content or "")


class ChannelTimeTest(BotTestCase):
    async def run_cmd(self, date=None, member=None):
        it = make_interaction(make_member(ME))
        await bot.channeltime_cmd.callback(it, date=date, member=member)
        return it

    async def test_per_channel_sorted_with_total(self):
        add(self.db, ME, CH_STUDY, "study", self.y0 + 3600, self.y0 + 3600 + 600)        # 10m
        add(self.db, ME, CH_REST, "rest", self.y0 + 7200, self.y0 + 7200 + 1800)          # 30m
        add(self.db, ME, CH_STUDY, "study", self.y0 + 9000, self.y0 + 9000 + 300)         # 5m，同頻道加總
        add(self.db, ME, CH_STUDY2, "study", self.y0 + 10000, self.y0 + 10000 + 60)       # 1m
        add(self.db, OTHER, CH_STUDY, "study", self.y0 + 3600, self.y0 + 7200)            # 別人，不算
        it = await self.run_cmd(self.yesterday.isoformat())
        embed = sent(it)[1]["embed"]
        self.assertEqual(embed.description.split("\n"), [
            f"☕ <#{CH_REST}>　**30m00s**",
            f"📚 <#{CH_STUDY}>　**15m00s**",
            f"📚 <#{CH_STUDY2}>　**1m00s**",
            "",
            "⏱️ 合計 **46m00s**",
        ])
        self.assertIn(f"{self.yesterday:%m/%d}", embed.title)

    async def test_session_crossing_midnight_is_split(self):
        # 前天 23:00 到昨天 01:00，只算昨天的 1 小時
        add(self.db, ME, CH_STUDY, "study", self.y0 - 3600, self.y0 + 3600)
        it = await self.run_cmd(self.yesterday.isoformat())
        self.assertIn("1h00m00s", sent(it)[1]["embed"].description.split("\n")[0])

    async def test_open_session_counts_until_now(self):
        t0 = day_start_ts(self.today, bot.TZ)
        add(self.db, ME, CH_STUDY, "study", t0, None)
        with patch.object(bot.time, "time", return_value=t0 + 125):
            it = await self.run_cmd()
        self.assertIn("2m05s", sent(it)[1]["embed"].description)

    async def test_empty_day(self):
        it = await self.run_cmd(self.yesterday.isoformat())
        self.assertEqual(sent(it)[1]["embed"].description, "這天沒有任何紀錄")

    async def test_bad_date(self):
        it = await self.run_cmd("2026/10/04")
        content, kwargs = sent(it)
        self.assertIn("日期格式錯誤", content)
        self.assertTrue(kwargs["ephemeral"])

    async def test_other_member(self):
        add(self.db, OTHER, CH_REST, "rest", self.y0, self.y0 + 60)
        it = await self.run_cmd(self.yesterday.isoformat(), member=make_member(OTHER))
        self.assertIn(f"<#{CH_REST}>", sent(it)[1]["embed"].description)


class DailyReportTest(BotTestCase):
    def report(self, day=None, now=None):
        day = day or self.yesterday
        return bot.build_daily_report(self.db, G, day, now or self.y0 + 3 * 86400)

    def test_sorted_by_total_with_categories(self):
        add(self.db, ME, CH_STUDY, "study", self.y0, self.y0 + 3600)          # 1h
        add(self.db, ME, CH_REST, "rest", self.y0 + 3600, self.y0 + 4200)     # 10m
        add(self.db, OTHER, CH_STUDY, "study", self.y0, self.y0 + 7200)       # 2h
        lines = self.report().description.split("\n")
        self.assertEqual(lines, [
            f"🥇 <@{OTHER}>　**2h00m00s**",
            "　📚 2h00m00s",
            f"🥈 <@{ME}>　**1h10m00s**",
            "　📚 1h00m00s　☕ 10m00s",
        ])

    def test_category_totals_and_footer(self):
        add(self.db, ME, CH_STUDY, "study", self.y0, self.y0 + 3600)
        add(self.db, OTHER, CH_STUDY, "study", self.y0, self.y0 + 60)
        embed = self.report()
        fields = {f.name: f.value for f in embed.fields}
        self.assertEqual(fields["📚 讀書"], "1h01m00s")
        self.assertEqual(fields["☕ 休息"], "0s")
        self.assertIn("共 2 人", embed.footer.text)

    def test_only_counts_that_day(self):
        add(self.db, ME, CH_STUDY, "study", self.y0 - 3600, self.y0 + 60)            # 跨入昨天 1 分鐘
        add(self.db, OTHER, CH_STUDY, "study", self.y0 + 86400 - 60, self.y0 + 86400 + 3600)  # 跨出昨天 1 分鐘
        add(self.db, OTHER + 1, CH_STUDY, "study", self.y0 - 7200, self.y0 - 3600)   # 前天
        embed = self.report()
        self.assertEqual(embed.description.count("<@"), 2)
        self.assertEqual({f.name: f.value for f in embed.fields}["📚 讀書"], "2m00s")

    def test_other_guild_excluded(self):
        self.db.open_session(G + 1, ME, CH_STUDY, "study", self.y0)
        self.db.close_user_sessions(G + 1, ME, self.y0 + 60)
        self.assertEqual(self.report().description, "這天沒有任何人進入追蹤中的語音頻道")

    def test_open_session_counts_until_now(self):
        add(self.db, ME, CH_STUDY, "study", self.y0, None)
        embed = self.report(now=self.y0 + 90)
        self.assertIn("**1m30s**", embed.description)

    def test_medals_and_ranks(self):
        for i in range(5):
            add(self.db, ME + i, CH_STUDY, "study", self.y0, self.y0 + 100 - i)
        lines = self.report().description.split("\n")[::2]
        self.assertEqual([l.split(" ")[0] for l in lines], ["🥇", "🥈", "🥉", "`#4`", "`#5`"])

    def test_long_ranking_is_truncated_within_embed_limit(self):
        n = 200
        for i in range(n):
            add(self.db, 10**17 + i, CH_STUDY, "study", self.y0, self.y0 + 1000 + i)
        embed = self.report()
        self.assertLessEqual(len(embed.description), 4096)
        self.assertRegex(embed.description.split("\n")[-1], r"^…還有 \d+ 人$")
        shown = embed.description.count("<@")
        self.assertEqual(int(embed.description.split(" ")[-2]), n - shown)
        self.assertIn(f"共 {n} 人", embed.footer.text)
        self.assertLessEqual(len(embed), 6000)


class PostDailyReportsTest(BotTestCase):
    def setUp(self):
        super().setUp()
        self.channel = MagicMock()
        self.channel.id = 555
        self.channel.send = AsyncMock()
        p = patch.object(bot.bot, "get_channel", return_value=self.channel)
        self.get_channel = p.start()
        self.addCleanup(p.stop)

    def last(self):
        return self.db.get_report_settings()[0]["report_last"]

    async def test_waits_until_configured_time(self):
        self.db.set_report_channel(G, 555, self.yesterday.isoformat(), 8 * 60 + 30)
        self.at(8, 29)
        await bot.bot.post_daily_reports()
        self.channel.send.assert_not_called()
        self.assertEqual(self.last(), self.yesterday.isoformat())

    async def test_posts_at_configured_time(self):
        self.db.set_report_channel(G, 555, self.yesterday.isoformat(), 8 * 60 + 30)
        self.at(8, 30)
        await bot.bot.post_daily_reports()
        self.channel.send.assert_called_once()
        self.assertIn(f"{self.yesterday:%m/%d}", self.channel.send.call_args.kwargs["embed"].title)
        self.assertEqual(self.last(), self.today.isoformat())

    async def test_catches_up_later_same_day(self):
        self.db.set_report_channel(G, 555, self.yesterday.isoformat(), 8 * 60)
        self.at(15)
        await bot.bot.post_daily_reports()
        self.channel.send.assert_called_once()

    async def test_forum_channel_creates_post(self):
        forum = MagicMock(spec=discord.ForumChannel)
        forum.id = 555
        forum.create_thread = AsyncMock()
        self.get_channel.return_value = forum
        self.db.set_report_channel(G, 555, self.yesterday.isoformat())
        await bot.bot.post_daily_reports()
        forum.create_thread.assert_awaited_once()
        kwargs = forum.create_thread.call_args.kwargs
        self.assertEqual(kwargs["name"], kwargs["embed"].title)
        self.assertIn(f"{self.yesterday:%m/%d}", kwargs["name"])
        self.assertFalse(kwargs["allowed_mentions"].users)

    async def test_posts_yesterday_once(self):
        add(self.db, ME, CH_STUDY, "study", self.y0, self.y0 + 60)
        self.db.set_report_channel(G, 555, self.yesterday.isoformat())
        await bot.bot.post_daily_reports()
        self.channel.send.assert_called_once()
        kwargs = self.channel.send.call_args.kwargs
        self.assertIn(f"{self.yesterday:%m/%d}", kwargs["embed"].title)
        self.assertIn(f"<@{ME}>", kwargs["embed"].description)
        self.assertFalse(kwargs["allowed_mentions"].users)
        self.assertEqual(self.last(), self.today.isoformat())
        self.get_channel.assert_called_with(555)

        await bot.bot.post_daily_reports()  # 同一天不重複發送
        self.channel.send.assert_called_once()

    async def test_catches_up_after_multi_day_downtime(self):
        self.db.set_report_channel(G, 555, (self.today - timedelta(days=5)).isoformat())
        await bot.bot.post_daily_reports()
        self.channel.send.assert_called_once()
        self.assertIn(f"{self.yesterday:%m/%d}", self.channel.send.call_args.kwargs["embed"].title)

    async def test_skips_when_already_done_today(self):
        self.db.set_report_channel(G, 555, self.today.isoformat())
        await bot.bot.post_daily_reports()
        self.channel.send.assert_not_called()

    async def test_null_last_posts(self):
        self.db.set_report_channel(G, 555, None)
        await bot.bot.post_daily_reports()
        self.channel.send.assert_called_once()

    async def test_disabled_guild_is_skipped(self):
        self.db.set_report_channel(G, 555, self.yesterday.isoformat())
        self.db.set_report_channel(G, None)
        await bot.bot.post_daily_reports()
        self.channel.send.assert_not_called()

    async def test_missing_channel_marks_done_without_error(self):
        self.get_channel.return_value = None
        self.db.set_report_channel(G, 555, self.yesterday.isoformat())
        with self.assertLogs("timerecord", "WARNING"):
            await bot.bot.post_daily_reports()
        self.assertEqual(self.last(), self.today.isoformat())

    async def test_send_failure_is_logged_and_not_retried(self):
        self.channel.send.side_effect = discord.Forbidden(MagicMock(status=403), "no perms")
        self.db.set_report_channel(G, 555, self.yesterday.isoformat())
        with self.assertLogs("timerecord", "ERROR"):
            await bot.bot.post_daily_reports()
        self.assertEqual(self.last(), self.today.isoformat())
        await bot.bot.post_daily_reports()
        self.channel.send.assert_called_once()

    async def test_multiple_guilds(self):
        self.db.set_report_channel(G, 555, self.yesterday.isoformat())
        self.db.set_report_channel(G + 1, 556, self.yesterday.isoformat())
        await bot.bot.post_daily_reports()
        self.assertEqual(self.channel.send.call_count, 2)

    async def test_heartbeat_posts_only_when_ready_and_survives_errors(self):
        with patch.object(bot.bot, "post_daily_reports", AsyncMock()) as post, \
             patch.object(bot.bot, "is_ready", return_value=False):
            await bot.bot.heartbeat.coro(bot.bot)
            post.assert_not_called()
        self.assertIsNotNone(self.db.get_meta("last_alive"))
        with patch.object(bot.bot, "post_daily_reports", AsyncMock(side_effect=RuntimeError)) as post, \
             patch.object(bot.bot, "is_ready", return_value=True), \
             self.assertLogs("timerecord", "ERROR"):
            await bot.bot.heartbeat.coro(bot.bot)  # 不應丟出例外
            post.assert_awaited_once()


class AdminCommandsTest(BotTestCase):
    async def test_privacy_set_and_show(self):
        it = make_interaction(make_member(ME, admin=True))
        await bot.privacy_cmd.callback(it, mode=app_commands.Choice(name="x", value="private"))
        self.assertEqual(self.db.get_privacy(G), "private")
        self.assertIn("私人", sent(it)[0])

        it = make_interaction(make_member(ME, admin=True))
        await bot.privacy_cmd.callback(it, mode=None)
        content, kwargs = sent(it)
        self.assertIn("私人", content)
        self.assertTrue(kwargs["ephemeral"])

    def channel_with_perms(self, ok=True):
        ch = MagicMock()
        ch.id = 555
        ch.mention = "<#555>"
        perms = MagicMock(view_channel=True, send_messages=ok, embed_links=True)
        ch.permissions_for.return_value = perms
        return ch

    def forum_with_perms(self, require_tag=False, media=False):
        ch = MagicMock(spec=discord.ForumChannel)
        ch.id = 555
        ch.mention = "<#555>"
        ch.is_media.return_value = media
        ch.flags.require_tag = require_tag
        ch.permissions_for.return_value = MagicMock(view_channel=True, send_messages=True, embed_links=True)
        return ch

    async def test_report_set(self):
        it = make_interaction(make_member(ME, admin=True))
        await bot.report_set_cmd.callback(it, channel=self.channel_with_perms())
        rows = self.db.get_report_settings()
        self.assertEqual(rows[0]["report_channel_id"], 555)
        self.assertEqual(rows[0]["report_time"], 0)
        # 今天的 00:00 已經過了，視為已處理，所以不會在設定後立刻補發昨天的排行榜
        self.assertEqual(rows[0]["report_last"], self.today.isoformat())

    async def test_report_set_time_later_today_starts_today(self):
        self.at(7, 0)
        it = make_interaction(make_member(ME, admin=True))
        await bot.report_set_cmd.callback(it, channel=self.channel_with_perms(), at="8:30")
        row = self.db.get_report_settings()[0]
        self.assertEqual(row["report_time"], 510)
        self.assertEqual(row["report_last"], self.yesterday.isoformat())
        self.assertIn("08:30", sent(it)[0])
        self.assertIn("今天", sent(it)[0])

    async def test_report_set_time_already_passed_starts_tomorrow(self):
        self.at(9, 0)
        it = make_interaction(make_member(ME, admin=True))
        await bot.report_set_cmd.callback(it, channel=self.channel_with_perms(), at="08:30")
        self.assertEqual(self.db.get_report_settings()[0]["report_last"], self.today.isoformat())
        self.assertIn("明天", sent(it)[0])

    async def test_report_set_rejects_bad_time(self):
        for bad in ["24:00", "8", "abc", "12:60"]:
            it = make_interaction(make_member(ME, admin=True))
            await bot.report_set_cmd.callback(it, channel=self.channel_with_perms(), at=bad)
            self.assertTrue(sent(it)[1]["ephemeral"], bad)
        self.assertEqual(self.db.get_report_settings(), [])

    async def test_report_set_forum(self):
        it = make_interaction(make_member(ME, admin=True))
        await bot.report_set_cmd.callback(it, channel=self.forum_with_perms())
        self.assertEqual(self.db.get_report_settings()[0]["report_channel_id"], 555)

    async def test_report_set_rejects_forum_requiring_tags_and_media(self):
        for ch in [self.forum_with_perms(require_tag=True), self.forum_with_perms(media=True)]:
            it = make_interaction(make_member(ME, admin=True))
            await bot.report_set_cmd.callback(it, channel=ch)
            self.assertTrue(sent(it)[1]["ephemeral"])
        self.assertEqual(self.db.get_report_settings(), [])

    async def test_report_set_requires_bot_permissions(self):
        it = make_interaction(make_member(ME, admin=True))
        await bot.report_set_cmd.callback(it, channel=self.channel_with_perms(ok=False))
        self.assertEqual(self.db.get_report_settings(), [])
        self.assertTrue(sent(it)[1]["ephemeral"])

    async def test_report_off(self):
        self.db.set_report_channel(G, 555, self.today.isoformat())
        it = make_interaction(make_member(ME, admin=True))
        await bot.report_off_cmd.callback(it)
        self.assertEqual(self.db.get_report_settings(), [])

    async def test_report_preview_is_today_and_ephemeral(self):
        add(self.db, ME, CH_STUDY, "study", day_start_ts(self.today, bot.TZ), None)
        it = make_interaction(make_member(ME, admin=True))
        await bot.report_preview_cmd.callback(it)
        kwargs = sent(it)[1]
        self.assertTrue(kwargs["ephemeral"])
        self.assertIn(f"{self.today:%m/%d}", kwargs["embed"].title)
        self.assertIn(f"<@{ME}>", kwargs["embed"].description)


class CommandRegistrationTest(unittest.TestCase):
    def test_new_commands_registered(self):
        tree = bot.bot.tree
        for name in ["channeltime", "privacy", "report"]:
            self.assertIsNotNone(tree.get_command(name), name)
        report = tree.get_command("report")
        self.assertEqual(sorted(c.name for c in report.commands), ["off", "preview", "set"])
        params = {p.display_name: p for p in report.get_command("set").parameters}
        self.assertEqual(sorted(params), ["channel", "time"])
        self.assertFalse(params["time"].required)
        self.assertEqual(
            set(params["channel"].channel_types),
            {discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.forum, discord.ChannelType.media},
        )

    def test_admin_commands_need_manage_guild(self):
        for name in ["privacy", "report"]:
            cmd = bot.bot.tree.get_command(name)
            self.assertTrue(cmd.default_permissions.manage_guild, name)
            self.assertTrue(cmd.guild_only, name)

    def test_channeltime_is_open_to_everyone(self):
        cmd = bot.bot.tree.get_command("channeltime")
        self.assertIsNone(cmd.default_permissions)
        self.assertTrue(cmd.guild_only)


if __name__ == "__main__":
    unittest.main()
