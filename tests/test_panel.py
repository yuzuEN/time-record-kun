"""打卡面板（/panel）的測試。執行：python -m unittest discover tests"""
import asyncio
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

    def test_guild_open_sessions_listed_in_join_order(self):
        self.db.open_session(G, OTHER, PANEL_CH, "study", T0 + 10, source="button")
        self.db.open_session(G, ME, PANEL_CH, "study", T0, source="button")
        self.db.open_session(G, ME + 1, VOICE_CH, "study", T0 + 5, source="voice")
        self.db.open_session(G, ME + 2, VOICE_CH, "study", T0, source="voice")
        self.db.close_user_sessions(G, ME + 2, T0 + 1)
        self.db.open_session(G + 1, ME, PANEL_CH, "study", T0, source="button")
        self.assertEqual([r["user_id"] for r in self.db.get_guild_open_sessions(G)], [ME, ME + 1, OTHER])

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

    def test_nudge_channel_keeps_other_settings(self):
        self.assertIsNone(self.db.get_nudge_channel(G))
        self.db.set_privacy(G, "private")
        self.db.set_nudge_channel(G, 777)
        self.assertEqual(self.db.get_nudge_channel(G), 777)
        self.assertEqual(self.db.get_privacy(G), "private")
        self.db.set_nudge_channel(G, None)
        self.assertIsNone(self.db.get_nudge_channel(G))

    def test_migrates_guild_settings_without_nudge_channel(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, privacy TEXT NOT NULL DEFAULT 'public', "
                "report_channel_id INTEGER, report_time INTEGER NOT NULL DEFAULT 0, report_last TEXT)"
            )
            conn.execute("INSERT INTO guild_settings (guild_id, privacy) VALUES (1, 'private')")
            conn.commit()
            conn.close()
            db = Database(path)
            self.assertIsNone(db.get_nudge_channel(1))
            self.assertEqual(db.get_privacy(1), "private")
            db.conn.close()

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
            (bot.bot, "request_panel_refresh", MagicMock()),
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
            [
                ("panel:work", "工作室"), ("panel:study", "讀書室"), ("panel:rest", "休息區"),
                ("panel:leave", "離開"), ("panel:nudge", "催讀書"),
            ],
        )

    async def test_zone_button_dispatches_its_category(self):
        with patch.object(bot, "panel_enter", AsyncMock()) as enter:
            it = make_interaction()
            await bot.PanelView().children[1].callback(it)
            enter.assert_awaited_once_with(it, "study")


class NudgeTest(PanelBotTestCase):
    def setUp(self):
        super().setUp()
        p = patch.object(bot.bot, "nudged_at", {})
        p.start()
        self.addCleanup(p.stop)
        self.target = MagicMock()
        self.target.id = OTHER
        self.target.mention = f"<@{OTHER}>"
        self.target.display_name = "小明"
        self.target.bot = False

    def make_it(self):
        it = make_interaction()
        it.channel.send = AsyncMock()
        it.response.edit_message = AsyncMock()
        return it

    def result(self, it):
        return it.response.edit_message.call_args.kwargs["content"]

    async def test_mentions_resting_member_with_rest_duration(self):
        self.db.open_session(G, OTHER, PANEL_CH, "rest", T0, source="button")
        it = self.make_it()
        await bot.send_nudge(it, self.target)
        args, kwargs = it.channel.send.call_args
        self.assertEqual(args[0], f"<@{OTHER}> 你已經休息 10m00s 了！還不讀書嗎")
        self.assertEqual(kwargs["allowed_mentions"].users, [self.target])
        self.assertEqual(kwargs["delete_after"], bot.NUDGE_DELETE_AFTER_SECONDS)
        self.assertIn("已提醒", self.result(it))

    async def test_voice_rest_counts_too(self):
        self.db.open_session(G, OTHER, VOICE_CH, "rest", T0)
        it = self.make_it()
        await bot.send_nudge(it, self.target)
        it.channel.send.assert_awaited_once()

    async def sent_text(self, custom_text=None):
        it = self.make_it()
        it.user.mention = f"<@{ME}>"
        await bot.send_nudge(it, self.target, custom_text)
        return it.channel.send.call_args.args[0]

    async def test_default_text_follows_member_status(self):
        for category, expected in (
            ("work", "你已經工作 10m00s 了！還不讀書嗎"),
            ("study", "你已經讀書 10m00s 了！繼續加油，不要停"),
            ("minecraft", "你已經玩 Minecraft 10m00s 了！還不讀書嗎"),
        ):
            with self.subTest(category):
                self.db.open_session(G, OTHER, PANEL_CH, category, T0, source="button")
                self.assertEqual(await self.sent_text(), f"<@{OTHER}> {expected}")
                self.db.close_user_sessions(G, OTHER, T0)
                bot.bot.nudged_at.clear()

    async def test_member_with_no_records_can_be_nudged(self):
        self.assertEqual(await self.sent_text(), f"<@{OTHER}> 你現在沒有在計時！還不讀書嗎")

    async def test_member_not_timing_shows_time_since_last_leave(self):
        self.db.add_session(G, OTHER, 0, "study", T0 - 100, T0 - 60, source="manual")
        self.db.add_session(G, OTHER, 0, "study", T0 - 500, T0 - 400, source="manual")
        self.db.add_session(G + 1, OTHER, 0, "study", T0, T0 + 500, source="manual")  # 別的伺服器不算
        self.assertEqual(await self.sent_text(), f"<@{OTHER}> 你已經離開 11m00s 了！還不讀書嗎")

    async def test_private_mode_hides_last_leave_time(self):
        self.db.add_session(G, OTHER, 0, "study", T0 - 100, T0 - 60, source="manual")
        self.db.set_privacy(G, "private")
        self.assertEqual(await self.sent_text(), f"<@{OTHER}> 你現在沒有在計時！還不讀書嗎")
        bot.bot.nudged_at.clear()
        self.assertIn("回來，已經一段時間了", await self.sent_text("回來，已經{時間}了"))

    async def test_configured_channel_gets_message_without_auto_delete(self):
        self.db.set_nudge_channel(G, 777)
        chat = MagicMock()
        chat.send = AsyncMock()
        chat.mention = "<#777>"
        it = self.make_it()
        with patch.object(bot.bot, "get_channel", return_value=chat) as get_channel:
            await bot.send_nudge(it, self.target)
        get_channel.assert_called_once_with(777)
        it.channel.send.assert_not_awaited()  # 面板頻道不會有新訊息
        self.assertEqual(chat.send.call_args.args[0], f"<@{OTHER}> 你現在沒有在計時！還不讀書嗎")
        self.assertIsNone(chat.send.call_args.kwargs["delete_after"])
        self.assertIn("<#777>", self.result(it))

    async def test_missing_configured_channel_does_not_fall_back_to_panel(self):
        self.db.set_nudge_channel(G, 777)
        it = self.make_it()
        with patch.object(bot.bot, "get_channel", return_value=None):
            await bot.send_nudge(it, self.target)
        it.channel.send.assert_not_awaited()
        self.assertIn("/nudge set", self.result(it))
        self.assertEqual(bot.bot.nudged_at, {})

    async def test_nudge_set_saves_channel(self):
        channel = MagicMock(id=777, mention="<#777>")
        it = make_interaction()
        await bot.nudge_set_cmd.callback(it, channel=channel)
        self.assertEqual(self.db.get_nudge_channel(G), 777)
        channel.permissions_for.assert_called_once_with(it.guild.me)
        self.assertIn("<#777>", reply(it))

    async def test_nudge_set_requires_bot_permissions(self):
        channel = MagicMock(id=777, mention="<#777>")
        channel.permissions_for.return_value.send_messages = False
        it = make_interaction()
        await bot.nudge_set_cmd.callback(it, channel=channel)
        self.assertIsNone(self.db.get_nudge_channel(G))
        self.assertIn("權限", reply(it))

    async def test_nudge_off_clears_channel(self):
        self.db.set_nudge_channel(G, 777)
        it = make_interaction()
        await bot.nudge_off_cmd.callback(it)
        self.assertIsNone(self.db.get_nudge_channel(G))

    async def test_bot_cannot_be_nudged(self):
        self.target.bot = True
        it = self.make_it()
        await bot.send_nudge(it, self.target)
        it.channel.send.assert_not_awaited()
        self.assertIn("機器人", self.result(it))

    async def test_cooldown_blocks_second_nudge_until_it_expires(self):
        self.db.open_session(G, OTHER, PANEL_CH, "rest", T0, source="button")
        await bot.send_nudge(self.make_it(), self.target)

        it = self.make_it()
        await bot.send_nudge(it, self.target)
        it.channel.send.assert_not_awaited()
        self.assertIn("5m00s", self.result(it))

        bot.time.time.return_value = self.now + bot.NUDGE_COOLDOWN_SECONDS
        it = self.make_it()
        await bot.send_nudge(it, self.target)
        it.channel.send.assert_awaited_once()

    async def test_failed_send_does_not_start_cooldown(self):
        self.db.open_session(G, OTHER, PANEL_CH, "rest", T0, source="button")
        it = self.make_it()
        it.channel.send.side_effect = bot.discord.Forbidden(MagicMock(status=403, reason="x"), "no")
        with self.assertLogs("timerecord", "ERROR"):
            await bot.send_nudge(it, self.target)
        self.assertIn("發送失敗", self.result(it))
        self.assertEqual(bot.bot.nudged_at, {})

    async def test_button_opens_private_member_picker(self):
        it = make_interaction()
        await bot.panel_nudge(it)
        kwargs = it.response.send_message.call_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertIsInstance(kwargs["view"].children[0], bot.discord.ui.UserSelect)

    async def pick_target(self):
        view = bot.NudgeView()
        select = view.children[0]
        select._values = [self.target]
        it = self.make_it()
        await select.callback(it)
        return it

    async def test_picking_member_previews_status_text_and_asks_which_text(self):
        self.db.open_session(G, OTHER, PANEL_CH, "study", T0, source="button")
        it = await self.pick_target()
        kwargs = it.response.edit_message.call_args.kwargs
        self.assertIsInstance(kwargs["view"], bot.NudgeTextView)
        self.assertIn("你已經讀書 10m00s 了", kwargs["content"])
        it.channel.send.assert_not_awaited()

    async def test_picking_bot_stops_early(self):
        self.target.bot = True
        it = await self.pick_target()
        self.assertIn("機器人", self.result(it))
        self.assertIsNone(it.response.edit_message.call_args.kwargs["view"])

    async def test_default_button_sends_default_text(self):
        self.db.open_session(G, OTHER, PANEL_CH, "rest", T0, source="button")
        view = bot.NudgeTextView(self.target)
        it = self.make_it()
        await view.children[0].callback(it)
        self.assertEqual(it.channel.send.call_args.args[0], f"<@{OTHER}> 你已經休息 10m00s 了！還不讀書嗎")
        self.assertTrue(view.is_finished())

    async def test_custom_button_opens_prefilled_modal_and_keeps_view_alive(self):
        self.db.open_session(G, OTHER, PANEL_CH, "work", T0, source="button")
        view = bot.NudgeTextView(self.target)
        it = self.make_it()
        it.response.send_modal = AsyncMock()
        await view.children[1].callback(it)
        modal = it.response.send_modal.call_args.args[0]
        self.assertIsInstance(modal, bot.NudgeTextModal)
        self.assertEqual(modal.text.default, "你已經工作 {時間} 了！還不讀書嗎")
        self.assertFalse(view.is_finished())

    async def submit_modal(self, value):
        self.db.open_session(G, OTHER, PANEL_CH, "rest", T0, source="button")
        view = bot.NudgeTextView(self.target)
        modal = bot.NudgeTextModal(self.target, view, "預設")
        modal.text._value = value
        it = self.make_it()
        it.user.mention = f"<@{ME}>"
        await modal.on_submit(it)
        self.assertTrue(view.is_finished())
        return it

    async def test_custom_text_fills_time_and_credits_author(self):
        it = await self.submit_modal("快回來！{時間} 夠久了 {時間}")
        args, kwargs = it.channel.send.call_args
        self.assertEqual(args[0], f"<@{OTHER}> 快回來！10m00s 夠久了 10m00s\n— <@{ME}>")
        self.assertEqual(kwargs["allowed_mentions"].users, [self.target])  # 只 @ 被催的人
        self.assertFalse(kwargs["allowed_mentions"].everyone)
        self.assertFalse(kwargs["allowed_mentions"].roles)

    async def test_custom_text_with_format_braces_is_literal(self):
        it = await self.submit_modal("{0} {x} 回來")
        self.assertIn("{0} {x} 回來", it.channel.send.call_args.args[0])

    async def test_blank_custom_text_falls_back_to_default(self):
        it = await self.submit_modal("   ")
        self.assertEqual(it.channel.send.call_args.args[0], f"<@{OTHER}> 你已經休息 10m00s 了！還不讀書嗎")

    async def test_rechecks_cooldown_when_custom_text_is_submitted(self):
        view = bot.NudgeTextView(self.target)
        modal = bot.NudgeTextModal(self.target, view, "預設")
        modal.text._value = "回來"
        bot.bot.nudged_at[(G, OTHER)] = self.now  # 打字期間別人先催了
        it = self.make_it()
        await modal.on_submit(it)
        it.channel.send.assert_not_awaited()
        self.assertIn("剛剛才被催過", self.result(it))


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
        bot.bot.request_panel_refresh.assert_called_once_with(G)

    async def test_untracked_voice_does_not_touch_button_session(self):
        self.db.open_session(G, ME, PANEL_CH, "work", T0, source="button")
        await bot.bot.on_voice_state_update(self.member(), self.state(None), self.state(VOICE_CH + 1))
        await bot.bot.on_voice_state_update(self.member(), self.state(VOICE_CH + 1), self.state(None))
        self.assertEqual(open_rows(self.db), [(ME, "work", "button", T0)])
        bot.bot.request_panel_refresh.assert_not_called()

    async def test_joining_and_leaving_zone_voice_refreshes_panel(self):
        self.db.set_channel(G, VOICE_CH, "rest")
        await bot.bot.on_voice_state_update(self.member(), self.state(None), self.state(VOICE_CH))
        await bot.bot.on_voice_state_update(self.member(), self.state(VOICE_CH), self.state(None))
        self.assertEqual(bot.bot.request_panel_refresh.call_count, 2)

    async def test_minecraft_voice_does_not_refresh_panel(self):
        self.db.set_channel(G, VOICE_CH, "minecraft")
        await bot.bot.on_voice_state_update(self.member(), self.state(None), self.state(VOICE_CH))
        await bot.bot.on_voice_state_update(self.member(), self.state(VOICE_CH), self.state(None))
        self.assertEqual(open_rows(self.db), [])
        bot.bot.request_panel_refresh.assert_not_called()


class PanelRefreshViewTest(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_updates_buttons_on_existing_panel(self):
        db = Database(":memory:")
        db.set_panel(G, PANEL_CH, 1)
        message = MagicMock()
        message.edit = AsyncMock()
        channel = MagicMock()
        channel.get_partial_message.return_value = message
        with patch.object(bot.bot, "db", db), patch.object(bot.bot, "get_partial_messageable", return_value=channel):
            await bot.bot.refresh_panel(G)
        view = message.edit.call_args.kwargs["view"]
        self.assertIn("panel:nudge", [b.custom_id for b in view.children])


class PanelRefreshThrottleTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target, attr, value in (
            (bot.bot, "refresh_panel", AsyncMock()),
            (bot.bot, "panel_refreshed_at", {}),
            (bot.bot, "panel_refresh_tasks", {}),
            (bot.time, "monotonic", MagicMock(return_value=1000.0)),
            (bot.asyncio, "sleep", AsyncMock()),
        ):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    async def run_pending(self):
        await asyncio.gather(*bot.bot.panel_refresh_tasks.values())

    async def test_first_refresh_is_immediate(self):
        bot.bot.request_panel_refresh(G)
        await self.run_pending()
        bot.asyncio.sleep.assert_awaited_once_with(0)
        bot.bot.refresh_panel.assert_awaited_once_with(G)
        self.assertEqual(bot.bot.panel_refresh_tasks, {})

    async def test_changes_within_interval_are_merged(self):
        bot.bot.panel_refreshed_at[G] = 1000.0 - 10
        for _ in range(5):
            bot.bot.request_panel_refresh(G)
        await self.run_pending()
        bot.asyncio.sleep.assert_awaited_once_with(bot.PANEL_REFRESH_SECONDS - 10)
        bot.bot.refresh_panel.assert_awaited_once_with(G)

    async def test_can_schedule_again_after_refresh(self):
        bot.bot.request_panel_refresh(G)
        await self.run_pending()
        bot.bot.request_panel_refresh(G)
        await self.run_pending()
        self.assertEqual(bot.bot.refresh_panel.await_count, 2)


class PanelEmbedTest(unittest.TestCase):
    def test_lists_members_per_zone(self):
        db = Database(":memory:")
        db.open_session(G, ME, PANEL_CH, "study", T0, source="button")
        db.open_session(G, OTHER, PANEL_CH, "study", T0 + 5, source="button")
        db.open_session(G, ME + 1, VOICE_CH, "work", T0)
        db.open_session(G, ME + 2, VOICE_CH, "study", T0 + 3)
        db.open_session(G, ME + 3, VOICE_CH, "minecraft", T0)  # Minecraft 不在面板上
        fields = {f.name: f.value for f in bot.build_panel_embed(db, G).fields}
        self.assertEqual(fields, {
            "💼 工作室（1）": f"🎧 <@{ME + 1}>　<t:{T0}:R>",
            "📚 讀書室（3）": f"<@{ME}>　<t:{T0}:R>\n🎧 <@{ME + 2}>　<t:{T0 + 3}:R>\n<@{OTHER}>　<t:{T0 + 5}:R>",
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
