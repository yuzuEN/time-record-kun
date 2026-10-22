"""SQLite 資料存取層。"""
import sqlite3
from typing import List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_map (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    category   TEXT    NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    category   TEXT    NOT NULL,
    join_ts    INTEGER NOT NULL,
    leave_ts   INTEGER,
    source     TEXT    NOT NULL DEFAULT 'voice'  -- voice：進出語音頻道；button：按打卡面板的按鈕
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (guild_id, user_id, join_ts);
CREATE INDEX IF NOT EXISTS idx_sessions_open ON sessions (leave_ts);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id          INTEGER PRIMARY KEY,
    privacy           TEXT NOT NULL DEFAULT 'public',  -- public：大家都能查大家；private：只有管理員能查別人
    report_channel_id INTEGER,                         -- 每日排行榜公告頻道，NULL 表示關閉
    report_time       INTEGER NOT NULL DEFAULT 0,      -- 每日排行榜公告時間，當地時間午夜後第幾分鐘
    report_last       TEXT                             -- 最後一次已處理每日排行榜的本地日期（YYYY-MM-DD）
);

CREATE TABLE IF NOT EXISTS panels (
    guild_id   INTEGER PRIMARY KEY,  -- 每個伺服器只保留一個打卡面板
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """替舊版資料庫補上後來新增的欄位。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(guild_settings)")}
        if "report_time" not in cols:
            self.conn.execute("ALTER TABLE guild_settings ADD COLUMN report_time INTEGER NOT NULL DEFAULT 0")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(sessions)")}
        if "source" not in cols:
            self.conn.execute("ALTER TABLE sessions ADD COLUMN source TEXT NOT NULL DEFAULT 'voice'")

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    # ---------- 頻道對應 ----------

    def set_channel(self, guild_id: int, channel_id: int, category: str) -> None:
        self._exec(
            "INSERT INTO channel_map (guild_id, channel_id, category) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id, channel_id) DO UPDATE SET category = excluded.category",
            (guild_id, channel_id, category),
        )

    def unset_channel(self, guild_id: int, channel_id: int) -> bool:
        cur = self._exec(
            "DELETE FROM channel_map WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        return cur.rowcount > 0

    def get_category(self, guild_id: int, channel_id: int) -> Optional[str]:
        row = self.conn.execute(
            "SELECT category FROM channel_map WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ).fetchone()
        return row["category"] if row else None

    def list_channels(self, guild_id: int) -> List[Tuple[int, str]]:
        rows = self.conn.execute(
            "SELECT channel_id, category FROM channel_map WHERE guild_id = ? ORDER BY category",
            (guild_id,),
        ).fetchall()
        return [(r["channel_id"], r["category"]) for r in rows]

    # ---------- 語音時段 ----------

    def open_session(
        self, guild_id: int, user_id: int, channel_id: int, category: str, ts: int, source: str = "voice"
    ) -> None:
        self._exec(
            "INSERT INTO sessions (guild_id, user_id, channel_id, category, join_ts, source) VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, channel_id, category, ts, source),
        )

    def close_user_sessions(self, guild_id: int, user_id: int, ts: int, source: Optional[str] = None) -> int:
        """結束某使用者在該伺服器所有尚未結束的時段；指定 source 時只結束該來源的時段。回傳結束的時段數。"""
        cur = self._exec(
            "UPDATE sessions SET leave_ts = MAX(join_ts, ?) "
            "WHERE guild_id = ? AND user_id = ? AND leave_ts IS NULL AND (? IS NULL OR source = ?)",
            (ts, guild_id, user_id, source, source),
        )
        return cur.rowcount

    def close_channel_sessions(self, guild_id: int, channel_id: int, ts: int) -> None:
        self._exec(
            "UPDATE sessions SET leave_ts = MAX(join_ts, ?) "
            "WHERE guild_id = ? AND channel_id = ? AND leave_ts IS NULL",
            (ts, guild_id, channel_id),
        )

    def close_session(self, session_id: int, ts: int) -> None:
        self._exec(
            "UPDATE sessions SET leave_ts = MAX(join_ts, ?) WHERE id = ?",
            (ts, session_id),
        )

    def get_open_sessions(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM sessions WHERE leave_ts IS NULL").fetchall()

    def get_open_session(self, guild_id: int, user_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE guild_id = ? AND user_id = ? AND leave_ts IS NULL "
            "ORDER BY join_ts DESC LIMIT 1",
            (guild_id, user_id),
        ).fetchone()

    def get_button_sessions(self, guild_id: int) -> List[sqlite3.Row]:
        """目前用打卡面板計時中的所有時段，先打卡的排前面。"""
        return self.conn.execute(
            "SELECT * FROM sessions WHERE guild_id = ? AND source = 'button' AND leave_ts IS NULL ORDER BY join_ts",
            (guild_id,),
        ).fetchall()

    def close_stale_button_sessions(self, now: int, max_seconds: int) -> List[int]:
        """打卡超過 max_seconds 還沒離開的時段，視為忘記按離開，以 join_ts + max_seconds 結束。回傳受影響的伺服器。"""
        with self.conn:
            guilds = [
                r["guild_id"] for r in self.conn.execute(
                    "SELECT DISTINCT guild_id FROM sessions "
                    "WHERE source = 'button' AND leave_ts IS NULL AND join_ts + ? <= ?",
                    (max_seconds, now),
                )
            ]
            self.conn.execute(
                "UPDATE sessions SET leave_ts = join_ts + ? "
                "WHERE source = 'button' AND leave_ts IS NULL AND join_ts + ? <= ?",
                (max_seconds, max_seconds, now),
            )
        return guilds

    def query_sessions(self, guild_id: int, user_id: Optional[int], start: int, end: int) -> List[sqlite3.Row]:
        """取得與 [start, end) 有重疊的時段；user_id 為 None 時取全伺服器。"""
        return self.conn.execute(
            "SELECT * FROM sessions WHERE guild_id = ? AND (? IS NULL OR user_id = ?) "
            "AND join_ts < ? AND (leave_ts IS NULL OR leave_ts > ?) ORDER BY join_ts",
            (guild_id, user_id, user_id, end, start),
        ).fetchall()

    def delete_range(self, guild_id: int, user_id: int, start: int, end: int) -> int:
        """刪除某使用者在 [start, end) 內的紀錄：區間內的時段刪掉，跨邊界的時段裁切或拆成兩段。回傳受影響的時段數。"""
        rows = self.query_sessions(guild_id, user_id, start, end)
        with self.conn:
            for s in rows:
                join, leave = s["join_ts"], s["leave_ts"]
                covers_tail = leave is not None and leave <= end  # 時段結尾落在刪除區間內
                if join >= start and covers_tail:
                    self.conn.execute("DELETE FROM sessions WHERE id = ?", (s["id"],))
                elif join >= start:
                    self.conn.execute("UPDATE sessions SET join_ts = ? WHERE id = ?", (end, s["id"]))
                elif covers_tail:
                    self.conn.execute("UPDATE sessions SET leave_ts = ? WHERE id = ?", (start, s["id"]))
                else:
                    # 刪除區間在時段中間：保留前半段，後半段另存一筆
                    self.conn.execute("UPDATE sessions SET leave_ts = ? WHERE id = ?", (start, s["id"]))
                    self.conn.execute(
                        "INSERT INTO sessions (guild_id, user_id, channel_id, category, join_ts, leave_ts, source) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (guild_id, user_id, s["channel_id"], s["category"], end, leave, s["source"]),
                    )
        return len(rows)

    # ---------- 伺服器設定 ----------

    def get_privacy(self, guild_id: int) -> str:
        row = self.conn.execute("SELECT privacy FROM guild_settings WHERE guild_id = ?", (guild_id,)).fetchone()
        return row["privacy"] if row else "public"

    def set_privacy(self, guild_id: int, privacy: str) -> None:
        self._exec(
            "INSERT INTO guild_settings (guild_id, privacy) VALUES (?, ?) "
            "ON CONFLICT (guild_id) DO UPDATE SET privacy = excluded.privacy",
            (guild_id, privacy),
        )

    def set_report_channel(
        self, guild_id: int, channel_id: Optional[int], last: Optional[str] = None, minute: int = 0
    ) -> None:
        """設定（或以 None 關閉）每日排行榜頻道；last 是視為已處理過的日期，避免設定後立刻補發；
        minute 是每天公告的時間（午夜後第幾分鐘）。"""
        self._exec(
            "INSERT INTO guild_settings (guild_id, report_channel_id, report_last, report_time) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (guild_id) DO UPDATE SET report_channel_id = excluded.report_channel_id, "
            "report_last = excluded.report_last, report_time = excluded.report_time",
            (guild_id, channel_id, last, minute),
        )

    def set_report_last(self, guild_id: int, last: str) -> None:
        self._exec("UPDATE guild_settings SET report_last = ? WHERE guild_id = ?", (last, guild_id))

    def get_report_channel(self, guild_id: int) -> Optional[int]:
        row = self.conn.execute(
            "SELECT report_channel_id FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return row["report_channel_id"] if row else None

    def get_report_settings(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT guild_id, report_channel_id, report_last, report_time FROM guild_settings "
            "WHERE report_channel_id IS NOT NULL"
        ).fetchall()

    # ---------- 打卡面板 ----------

    def set_panel(self, guild_id: int, channel_id: int, message_id: int) -> None:
        self._exec(
            "INSERT INTO panels (guild_id, channel_id, message_id) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id) DO UPDATE SET channel_id = excluded.channel_id, message_id = excluded.message_id",
            (guild_id, channel_id, message_id),
        )

    def get_panel(self, guild_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT channel_id, message_id FROM panels WHERE guild_id = ?", (guild_id,)
        ).fetchone()

    def clear_panel(self, guild_id: int) -> None:
        self._exec("DELETE FROM panels WHERE guild_id = ?", (guild_id,))

    # ---------- meta ----------

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
