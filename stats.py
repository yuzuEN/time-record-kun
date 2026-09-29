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
