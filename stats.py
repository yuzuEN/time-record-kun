"""統計計算：把時段依「本地日期」切割並加總。"""
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta, tzinfo
from typing import Dict, Iterable, Iterator, Tuple

WEEKDAYS = "一二三四五六日"


def day_start_ts(d: date, tz: tzinfo) -> int:
    """某個本地日期 00:00 的 Unix timestamp。"""
    return int(datetime.combine(d, dtime.min, tzinfo=tz).timestamp())


def local_date(ts: int, tz: tzinfo) -> date:
    return datetime.fromtimestamp(ts, tz).date()


def parse_clock(s: str) -> dtime:
    """解析 HH:MM 或 HH:MM:SS。"""
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s.strip(), fmt).time()
        except ValueError:
            pass
    raise ValueError(f"時間格式錯誤：{s}")


def parse_range(date_str: str, start_str: str, end_str: str, tz: tzinfo) -> Tuple[int, int]:
    """把 YYYY-MM-DD 與兩個 HH:MM[:SS] 轉成 timestamp 區間；結束時間早於開始時間時視為隔天。
    格式錯誤或開始等於結束時丟出 ValueError。"""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    t1 = parse_clock(start_str)
    t2 = parse_clock(end_str)
    if t1 == t2:
        raise ValueError("開始時間與結束時間相同")
    begin = datetime.combine(d, t1, tzinfo=tz)
    finish = datetime.combine(d + timedelta(days=1) if t2 < t1 else d, t2, tzinfo=tz)
    return int(begin.timestamp()), int(finish.timestamp())


def split_by_day(start: int, end: int, tz: tzinfo) -> Iterator[Tuple[date, int]]:
    """把 [start, end) 切成每個本地日期各自的秒數，處理跨午夜的時段。"""
    cur = start
    while cur < end:
        d = local_date(cur, tz)
        seg_end = min(end, day_start_ts(d + timedelta(days=1), tz))
        yield d, seg_end - cur
        cur = seg_end


def aggregate(sessions: Iterable, range_start: int, range_end: int, now: int, tz: tzinfo) -> Dict[date, Dict[str, int]]:
    """回傳 {日期: {類別: 秒數}}；尚未結束的時段以 now 當作結束時間。"""
    result: Dict[date, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for s in sessions:
        a = max(s["join_ts"], range_start)
        b = min(s["leave_ts"] if s["leave_ts"] is not None else now, range_end)
        for d, sec in split_by_day(a, b, tz):
            result[d][s["category"]] += sec
    return result


def aggregate_by_user(sessions: Iterable, range_start: int, range_end: int, now: int) -> Dict[int, Dict[str, int]]:
    """回傳 {user_id: {類別: 秒數}}。"""
    result: Dict[int, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for s in sessions:
        a = max(s["join_ts"], range_start)
        b = min(s["leave_ts"] if s["leave_ts"] is not None else now, range_end)
        if b > a:
            result[s["user_id"]][s["category"]] += b - a
    return result


def fmt_duration(seconds: int) -> str:
    minutes, s = divmod(seconds, 60)
    h, m = divmod(minutes, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def fmt_date(d: date) -> str:
    return f"{d:%m/%d}({WEEKDAYS[d.weekday()]})"
