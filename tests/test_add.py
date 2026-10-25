"""手動補登（/add）的測試。執行：python -m unittest discover tests"""
import os
import unittest
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

# 必須在 import bot 之前設定，避免測試建立或寫入真正的資料庫檔案
os.environ["DB_PATH"] = ":memory:"
os.environ["TIMEZONE"] = "Asia/Taipei"

from discord import app_commands  # noqa: E402

import bot  # noqa: E402
from db import Database  # noqa: E402

G = 1
ME, ADMIN = 100, 900
VOICE_CH = 60
DAY = date(2026, 10, 25)


def ts(hour, minute=0):
    return int(datetime(DAY.year, DAY.month, DAY.day, hour, minute, tzinfo=bot.TZ).timestamp())


def make_member(uid):
    m = MagicMock()
    m.id = uid
    m.display_name = f"user{uid}"
    return m


def make_interaction():
    it = MagicMock()
    it.guild_id = G
    it.user = make_member(ADMIN)
    it.response.send_message = AsyncMock()
    it.response.edit_message = AsyncMock()
    return it


def content(mock):
    args, kwargs = mock.call_args
    return args[0] if args else kwargs.get("content")


STUDY = app_commands.Choice(name="📚 讀書", value="study")


class AddCommandTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database(":memory:")
        for target, attr, value in (
            (bot.bot, "db", self.db),
            (bot.time, "time", MagicMock(return_value=ts(12))),
        ):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def rows(self):
        return [
            (r["user_id"], r["channel_id"], r["category"], r["join_ts"], r["leave_ts"], r["source"])
            for r in self.db.conn.execute("SELECT * FROM sessions ORDER BY join_ts")
        ]

    async def run_add(self, start, end, category=STUDY, day=DAY.isoformat()):
        it = make_interaction()
        await bot.add_cmd.callback(it, member=make_member(ME), category=category, date=day, start=start, end=end)
        return it

    async def confirm(self, it):
        view = it.response.send_message.call_args.kwargs["view"]
        click = make_interaction()
        await view.confirm.callback(click)
        return click

    async def test_confirm_adds_manual_session(self):
        it = await self.run_add("07:00", "08:40")
        self.assertTrue(it.response.send_message.call_args.kwargs["ephemeral"])
        self.assertIn("1h40m00s", content(it.response.send_message))
        self.assertEqual(self.rows(), [])  # 確認前不寫入

        click = await self.confirm(it)
        self.assertEqual(self.rows(), [(ME, bot.MANUAL_CHANNEL_ID, "study", ts(7), ts(8, 40), "manual")])
        self.assertIn("已補登", content(click.response.edit_message))

    async def test_cancel_adds_nothing(self):
        it = await self.run_add("07:00", "08:40")
        view = it.response.send_message.call_args.kwargs["view"]
        await view.cancel.callback(make_interaction())
        self.assertEqual(self.rows(), [])

    async def test_overlap_with_existing_session_is_rejected(self):
        self.db.add_session(G, ME, VOICE_CH, "work", ts(8), ts(9), source="voice")
        it = await self.run_add("07:00", "08:40")
        msg = content(it.response.send_message)
        self.assertIn("已經有 1 段紀錄", msg)
        self.assertIn(f"<#{VOICE_CH}>", msg)
        self.assertNotIn("view", it.response.send_message.call_args.kwargs)
        self.assertEqual(len(self.rows()), 1)

    async def test_overlap_with_open_session_is_rejected(self):
        self.db.open_session(G, ME, VOICE_CH, "study", ts(6))
        it = await self.run_add("07:00", "08:40")
        self.assertIn("進行中", content(it.response.send_message))

    async def test_touching_existing_session_is_allowed(self):
        self.db.add_session(G, ME, VOICE_CH, "study", ts(6), ts(7), source="voice")
        await self.confirm(await self.run_add("07:00", "08:00"))
        self.assertEqual(len(self.rows()), 2)

    async def test_overlap_created_while_waiting_for_confirm_is_rejected(self):
        it = await self.run_add("07:00", "08:40")
        self.db.add_session(G, ME, VOICE_CH, "study", ts(8), ts(9), source="voice")
        click = await self.confirm(it)
        self.assertIn("沒有補登", content(click.response.edit_message))
        self.assertEqual(len(self.rows()), 1)

    async def test_future_end_is_rejected(self):
        it = await self.run_add("11:00", "13:00")
        self.assertIn("不能晚於現在", content(it.response.send_message))

    async def test_invalid_format_is_rejected(self):
        it = await self.run_add("7點", "08:40")
        self.assertIn("格式錯誤", content(it.response.send_message))

    async def test_end_before_start_spans_midnight(self):
        await self.confirm(await self.run_add("23:00", "01:00", day="2026-10-24"))
        self.assertEqual(self.rows()[0][3:5], (ts(0) - 3600, ts(1)))

    async def test_manual_session_counts_in_daily_report(self):
        await self.confirm(await self.run_add("07:00", "08:40"))
        embed = bot.build_daily_report(self.db, G, DAY, ts(12))
        self.assertIn(f"<@{ME}>", embed.description)
        self.assertIn("1h40m00s", embed.description)

    async def test_log_and_channeltime_show_manual_label(self):
        await self.confirm(await self.run_add("07:00", "08:40"))
        for cmd in (bot.log_cmd, bot.channeltime_cmd):
            it = make_interaction()
            it.user = make_member(ME)
            with patch.object(bot, "ensure_can_view", AsyncMock(return_value=True)):
                await cmd.callback(it, date=DAY.isoformat(), member=None)
            embed = it.response.send_message.call_args.kwargs["embed"]
            self.assertIn("手動補登", embed.description)
            self.assertNotIn("<#0>", embed.description)


if __name__ == "__main__":
    unittest.main()
