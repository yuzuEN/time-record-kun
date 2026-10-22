"""打卡面板（/panel）的測試。執行：python -m unittest discover tests"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# 必須在 import bot 之前設定，避免測試建立或寫入真正的資料庫檔案
os.environ["DB_PATH"] = ":memory:"
os.environ["TIMEZONE"] = "Asia/Taipei"

import bot  # noqa: E402
from db import Database  # noqa: E402

G = 1
ME, OTHER = 100, 200
PANEL_CH, VOICE_CH = 50, 60
T0 = 1_760_000_000


def make_interaction(user_id=ME):
    it = MagicMock()
    it.guild_id = G
    it.channel_id = PANEL_CH
    it.user.id = user_id
    it.response.send_message = AsyncMock()
    return it


def reply(interaction):
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral"), "面板的回覆應該只有本人看得到"
    return args[0] if args else kwargs.get("content")


def open_rows(db):
    return [
        (r["user_id"], r["category"], r["source"], r["join_ts"])
        for r in db.conn.execute("SELECT * FROM sessions WHERE leave_ts IS NULL ORDER BY id")
    ]


class PanelDbTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def test_source_defaults_to_voice(self):
        self.db.open_session(G, ME, VOICE_CH, "study", T0)
        self.assertEqual(self.db.get_open_session(G, ME)["source"], "voice")

    def test_close_by_source_only_closes_that_source(self):
        self.db.open_session(G, ME, VOICE_CH, "study", T0)
        self.db.open_session(G, ME, PANEL_CH, "work", T0, source="button")
        self.assertEqual(self.db.close_user_sessions(G, ME, T0 + 60, source="voice"), 1)
        self.assertEqual(open_rows(self.db), [(ME, "work", "button", T0)])
        self.assertEqual(self.db.close_user_sessions(G, ME, T0 + 60, source="voice"), 0)
        self.assertEqual(self.db.close_user_sessions(G, ME, T0 + 60), 1)
        self.assertEqual(open_rows(self.db), [])

    def test_button_sessions_listed_in_join_order(self):
        self.db.open_session(G, OTHER, PANEL_CH, "study", T0 + 10, source="button")
        self.db.open_session(G, ME, PANEL_CH, "study", T0, source="button")
        self.db.open_session(G, ME + 1, VOICE_CH, "study", T0, source="voice")
        self.db.open_session(G + 1, ME, PANEL_CH, "study", T0, source="button")
        self.assertEqual([r["user_id"] for r in self.db.get_button_sessions(G)], [ME, OTHER])

    def test_stale_button_sessions_are_closed_at_limit(self):
        self.db.open_session(G, ME, PANEL_CH, "study", T0, source="button")
        self.db.open_session(G, OTHER, PANEL_CH, "study", T0 + 3000, source="button")
        self.db.open_session(G, ME + 1, VOICE_CH, "study", T0, source="voice")
        self.assertEqual(self.db.close_stale_button_sessions(T0 + 3600, 3600), [G])
        row = self.db.conn.execute("SELECT leave_ts FROM sessions WHERE user_id = ?", (ME,)).fetchone()
        self.assertEqual(row["leave_ts"], T0 + 3600)
        self.assertEqual([u for u, *_ in open_rows(self.db)], [OTHER, ME + 1])
        self.assertEqual(self.db.close_stale_button_sessions(T0 + 3600, 3600), [])

    def test_delete_range_split_keeps_source(self):
        self.db.open_session(G, ME, PANEL_CH, "study", T0, source="button")
        self.db.close_user_sessions(G, ME, T0 + 300)
        self.db.delete_range(G, ME, T0 + 100, T0 + 200)
        sources = [r["source"] for r in self.db.conn.execute("SELECT source FROM sessions")]
        self.assertEqual(sources, ["button", "button"])

    def test_panel_set_replace_and_clear(self):
        self.assertIsNone(self.db.get_panel(G))
        self.db.set_panel(G, PANEL_CH, 1)
        self.db.set_panel(G, PANEL_CH + 1, 2)
        self.assertEqual(tuple(self.db.get_panel(G)), (PANEL_CH + 1, 2))
        self.db.clear_panel(G)
        self.assertIsNone(self.db.get_panel(G))

    def test_migrates_sessions_without_source(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, "
                "user_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, category TEXT NOT NULL, "
                "join_ts INTEGER NOT NULL, leave_ts INTEGER)"
            )
            conn.execute("INSERT INTO sessions (guild_id, user_id, channel_id, category, join_ts) VALUES (1, 2, 3, 'study', 10)")
            conn.commit()
            conn.close()
            db = Database(path)
            self.assertEqual(db.get_open_session(1, 2)["source"], "voice")
            db.conn.close()


class PanelBotTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database(":memory:")
        for target, attr, value in (
            (bot.bot, "db", self.db),
            (bot.bot, "refresh_panel", AsyncMock()),
            (bot.time, "time", MagicMock(return_value=T0 + 600)),
        ):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.now = T0 + 600


class PanelButtonTest(PanelBotTestCase):
    async def test_enter_starts_button_session(self):
        it = make_interaction()
        await bot.panel_enter(it, "study")
        self.assertEqual(open_rows(self.db), [(ME, "study", "button", self.now)])
        self.assertEqual(self.db.get_open_session(G, ME)["channel_id"], PANEL_CH)
        self.assertIn("📚 讀書室", reply(it))
        bot.bot.refresh_panel.assert_awaited_once_with(G)

    async def test_switch_zone_closes_previous(self):
        self.db.open_session(G, ME, PANEL_CH, "work", T0, source="button")
        it = make_interaction()
        await bot.panel_enter(it, "rest")
        self.assertEqual(open_rows(self.db), [(ME, "rest", "button", self.now)])
        self.assertIn("💼 工作室 待了 10m00s", reply(it))

    async def test_same_zone_shows_elapsed_without_restarting(self):
        self.db.open_session(G, ME, PANEL_CH, "study", T0, source="button")
        it = make_interaction()
        await bot.panel_enter(it, "study")
        self.assertEqual(open_rows(self.db), [(ME, "study", "button", T0)])
        self.assertIn("10m00s", reply(it))
        bot.bot.refresh_panel.assert_not_awaited()

    async def test_voice_session_blocks_panel(self):
        self.db.open_session(G, ME, VOICE_CH, "minecraft", T0)
        it = make_interaction()
        await bot.panel_enter(it, "study")
        self.assertEqual(open_rows(self.db), [(ME, "minecraft", "voice", T0)])
        self.assertIn("離開語音頻道", reply(it))

    async def test_leave_closes_button_session(self):
        self.db.open_session(G, ME, PANEL_CH, "study", T0, source="button")
        it = make_interaction()
        await bot.panel_leave(it)
        self.assertEqual(open_rows(self.db), [])
        self.assertIn("10m00s", reply(it))
        bot.bot.refresh_panel.assert_awaited_once_with(G)

    async def test_leave_without_button_session(self):
        self.db.open_session(G, ME, VOICE_CH, "study", T0)
        it = make_interaction()
        await bot.panel_leave(it)
        self.assertEqual(open_rows(self.db), [(ME, "study", "voice", T0)])
        self.assertIn("沒有用面板打卡", reply(it))


class PanelViewTest(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_buttons(self):
        view = bot.PanelView()
        self.assertTrue(view.is_persistent())
        self.assertEqual(
            [(b.custom_id, b.label) for b in view.children],
            [("panel:work", "工作室"), ("panel:study", "讀書室"), ("panel:rest", "休息區"), ("panel:leave", "離開")],
        )

    async def test_zone_button_dispatches_its_category(self):
        with patch.object(bot, "panel_enter", AsyncMock()) as enter:
            it = make_interaction()
            await bot.PanelView().children[1].callback(it)
            enter.assert_awaited_once_with(it, "study")


class VoicePriorityTest(PanelBotTestCase):
    def member(self):
        m = MagicMock()
        m.bot = False
        m.id = ME
        m.guild.id = G
        return m

    def state(self, channel_id):
        s = MagicMock()
        s.channel = None
        if channel_id is not None:
            s.channel = MagicMock()
            s.channel.id = channel_id
        return s

    async def test_joining_tracked_voice_ends_button_session(self):
        self.db.set_channel(G, VOICE_CH, "study")
        self.db.open_session(G, ME, PANEL_CH, "work", T0, source="button")
        await bot.bot.on_voice_state_update(self.member(), self.state(None), self.state(VOICE_CH))
        self.assertEqual(open_rows(self.db), [(ME, "study", "voice", self.now)])
        bot.bot.refresh_panel.assert_awaited_once_with(G)

    async def test_untracked_voice_does_not_touch_button_session(self):
        self.db.open_session(G, ME, PANEL_CH, "work", T0, source="button")
        await bot.bot.on_voice_state_update(self.member(), self.state(None), self.state(VOICE_CH + 1))
        await bot.bot.on_voice_state_update(self.member(), self.state(VOICE_CH + 1), self.state(None))
        self.assertEqual(open_rows(self.db), [(ME, "work", "button", T0)])


class PanelEmbedTest(unittest.TestCase):
    def test_lists_members_per_zone(self):
        db = Database(":memory:")
        db.open_session(G, ME, PANEL_CH, "study", T0, source="button")
        db.open_session(G, OTHER, PANEL_CH, "study", T0 + 5, source="button")
        db.open_session(G, ME + 1, VOICE_CH, "work", T0)  # 語音頻道的人不列在面板上
        fields = {f.name: f.value for f in bot.build_panel_embed(db, G).fields}
        self.assertEqual(fields, {
            "💼 工作室（0）": "—",
            "📚 讀書室（2）": f"<@{ME}>　<t:{T0}:R>\n<@{OTHER}>　<t:{T0 + 5}:R>",
            "☕ 休息區（0）": "—",
        })

    def test_long_list_is_truncated(self):
        db = Database(":memory:")
        for uid in range(1000, 1100):
            db.open_session(G, uid, PANEL_CH, "rest", T0, source="button")
        value = {f.name: f.value for f in bot.build_panel_embed(db, G).fields}["☕ 休息區（100）"]
        self.assertLessEqual(len(value), 1024)
        self.assertTrue(value.endswith("人"))


if __name__ == "__main__":
    unittest.main()
