"""/delete 相關邏輯的單元測試。執行：python -m unittest discover tests"""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from db import Database
from stats import parse_range

G, U = 1, 100  # 伺服器、使用者
CH = 9


class DeleteRangeTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def add(self, join, leave, user=U, guild=G, category="study"):
        self.db.open_session(guild, user, CH, category, join)
        if leave is not None:
            self.db.close_user_sessions(guild, user, leave)

    def rows(self, user=U, guild=G):
        return [
            (r["join_ts"], r["leave_ts"])
            for r in self.db.conn.execute(
                "SELECT join_ts, leave_ts FROM sessions WHERE guild_id = ? AND user_id = ? ORDER BY join_ts",
                (guild, user),
            )
        ]

    def test_session_fully_inside_is_deleted(self):
        self.add(200, 300)
        self.assertEqual(self.db.delete_range(G, U, 100, 400), 1)
        self.assertEqual(self.rows(), [])

    def test_range_equal_to_session_is_deleted(self):
        self.add(100, 400)
        self.db.delete_range(G, U, 100, 400)
        self.assertEqual(self.rows(), [])

    def test_trims_head(self):
        self.add(100, 400)
        self.db.delete_range(G, U, 50, 200)
        self.assertEqual(self.rows(), [(200, 400)])

    def test_trims_tail(self):
        self.add(100, 400)
        self.db.delete_range(G, U, 300, 500)
        self.assertEqual(self.rows(), [(100, 300)])

    def test_splits_session_when_range_is_in_the_middle(self):
        self.add(100, 1000, category="rest")
        self.db.delete_range(G, U, 300, 500)
        self.assertEqual(self.rows(), [(100, 300), (500, 1000)])
        cats = [r["category"] for r in self.db.conn.execute("SELECT category FROM sessions")]
        self.assertEqual(cats, ["rest", "rest"])

    def test_open_session_starting_inside_keeps_recording_after_range(self):
        self.add(200, None)
        self.db.delete_range(G, U, 100, 500)
        self.assertEqual(self.rows(), [(500, None)])
        self.assertIsNotNone(self.db.get_open_session(G, U))

    def test_open_session_spanning_range_is_split_and_stays_open(self):
        self.add(100, None)
        self.db.delete_range(G, U, 300, 500)
        self.assertEqual(self.rows(), [(100, 300), (500, None)])
        self.assertEqual(self.db.get_open_session(G, U)["join_ts"], 500)

    def test_multiple_sessions(self):
        self.add(0, 100)     # 不重疊
        self.add(150, 250)   # 裁尾
        self.add(300, 400)   # 刪除
        self.add(450, 600)   # 裁頭
        self.assertEqual(self.db.delete_range(G, U, 200, 500), 3)
        self.assertEqual(self.rows(), [(0, 100), (150, 200), (500, 600)])

    def test_touching_boundaries_are_untouched(self):
        self.add(0, 100)
        self.add(200, 300)
        self.assertEqual(self.db.delete_range(G, U, 100, 200), 0)
        self.assertEqual(self.rows(), [(0, 100), (200, 300)])

    def test_short_session_removed_with_second_precision(self):
        # 例如 21:50:05 ~ 21:50:57 的 52 秒紀錄，用秒數精確刪除，前後紀錄不受影響
        self.add(1000, 1005)
        self.add(1005, 1057)
        self.add(1057, 1100)
        self.db.delete_range(G, U, 1005, 1057)
        self.assertEqual(self.rows(), [(1000, 1005), (1057, 1100)])

    def test_other_users_and_guilds_are_untouched(self):
        self.add(100, 400)
        self.add(100, 400, user=U + 1)
        self.add(100, 400, guild=G + 1)
        self.db.delete_range(G, U, 0, 1000)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rows(user=U + 1), [(100, 400)])
        self.assertEqual(self.rows(guild=G + 1), [(100, 400)])


class ParseRangeTest(unittest.TestCase):
    TZ = ZoneInfo("Asia/Taipei")

    def ts(self, *args):
        return int(datetime(*args, tzinfo=self.TZ).timestamp())

    def test_same_day(self):
        self.assertEqual(
            parse_range("2026-10-04", "08:00", "10:30", self.TZ),
            (self.ts(2026, 10, 4, 8, 0), self.ts(2026, 10, 4, 10, 30)),
        )

    def test_end_before_start_means_next_day(self):
        self.assertEqual(
            parse_range("2026-10-04", "23:00", "08:30", self.TZ),
            (self.ts(2026, 10, 4, 23, 0), self.ts(2026, 10, 5, 8, 30)),
        )

    def test_equal_times_raise(self):
        # 開始等於結束不再被當成一整天，避免誤刪
        for t in ("21:50", "21:50:00"):
            with self.assertRaises(ValueError):
                parse_range("2026-10-04", t, t, self.TZ)

    def test_seconds(self):
        self.assertEqual(
            parse_range("2026-10-04", "21:50:08", "21:51", self.TZ),
            (self.ts(2026, 10, 4, 21, 50, 8), self.ts(2026, 10, 4, 21, 51, 0)),
        )

    def test_seconds_within_same_minute(self):
        start, end = parse_range("2026-10-04", "21:50:00", "21:50:59", self.TZ)
        self.assertEqual(end - start, 59)

    def test_seconds_crossing_midnight(self):
        _, end = parse_range("2026-10-04", "23:59:30", "00:00:10", self.TZ)
        self.assertEqual(end, self.ts(2026, 10, 5, 0, 0, 10))

    def test_month_rollover(self):
        _, end = parse_range("2026-10-31", "22:00", "01:00", self.TZ)
        self.assertEqual(end, self.ts(2026, 11, 1, 1, 0))

    def test_invalid_formats_raise(self):
        for args in [("2026/10/04", "08:00", "09:00"), ("2026-10-04", "8點", "09:00"),
                     ("2026-10-04", "08:00", "25:00"), ("2026-02-30", "08:00", "09:00"),
                     ("2026-10-04", "08:00:60", "09:00"), ("2026-10-04", "08", "09:00")]:
            with self.assertRaises(ValueError):
                parse_range(*args, self.TZ)


if __name__ == "__main__":
    unittest.main()
