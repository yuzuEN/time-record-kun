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
    leave_ts   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (guild_id, user_id, join_ts);
CREATE INDEX IF NOT EXISTS idx_sessions_open ON sessions (leave_ts);

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
        self.conn.commit()

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

    def open_session(self, guild_id: int, user_id: int, channel_id: int, category: str, ts: int) -> None:
        self._exec(
            "INSERT INTO sessions (guild_id, user_id, channel_id, category, join_ts) VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, channel_id, category, ts),
        )

    def close_user_sessions(self, guild_id: int, user_id: int, ts: int) -> None:
        """結束某使用者在該伺服器所有尚未結束的時段。"""
        self._exec(
            "UPDATE sessions SET leave_ts = MAX(join_ts, ?) "
            "WHERE guild_id = ? AND user_id = ? AND leave_ts IS NULL",
            (ts, guild_id, user_id),
        )

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
                        "INSERT INTO sessions (guild_id, user_id, channel_id, category, join_ts, leave_ts) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (guild_id, user_id, s["channel_id"], s["category"], end, leave),
                    )
        return len(rows)

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
