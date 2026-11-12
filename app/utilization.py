"""机队利用率核对：UTC 航段分批入仓 + 按 UTC 日覆盖秒数汇总。

业务规则
--------
- 上传格式与清洗导出一致：``tail_number,seq,dep_utc,arr_utc,duration_minutes``，
  时间为带时区的 ISO 8601（导出为 ``...Z``）；
- 同一批号重复上传 → 新文件**整体替换**旧批；文件只有表头 → **撤回**该批；
- 任一行非法 → **整批退回**，数据库不产生任何变更；
- 航段按 UTC 午夜拆分，区间**含起飞、不含落地**（半开区间 [dep, arr)）：
  恰好午夜落地的航段在落地日不计秒，恰好午夜起飞的航段计入起飞当日；
- 同一机尾同一 UTC 日期，所有批次的重叠时段只计一次（区间并集）；
- 每次提交只重算旧批/新批覆盖到的 (机尾, 日期) 组合；
- 批次表与汇总表在同一 SQLite 事务中提交；``BEGIN IMMEDIATE`` 保证并发
  换批的结果等同于逐批串行提交（写事务互斥，后提交者覆盖先提交者）。
"""
from __future__ import annotations

import csv
import io
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

REQUIRED_COLUMNS = ["tail_number", "seq", "dep_utc", "arr_utc", "duration_minutes"]

MAX_BATCH_ID_LEN = 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  batch_id     TEXT PRIMARY KEY,
  uploaded_at  TEXT NOT NULL,
  row_count    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
  batch_id    TEXT NOT NULL,
  tail_number TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  dep_utc     REAL NOT NULL,   -- epoch 秒
  arr_utc     REAL NOT NULL,   -- epoch 秒
  PRIMARY KEY (batch_id, tail_number, seq)
);
CREATE INDEX IF NOT EXISTS idx_segments_tail ON segments(tail_number, dep_utc, arr_utc);
CREATE TABLE IF NOT EXISTS coverage (
  tail_number     TEXT NOT NULL,
  utc_date        TEXT NOT NULL,   -- YYYY-MM-DD
  covered_seconds REAL NOT NULL,
  PRIMARY KEY (tail_number, utc_date)
);
"""


class CsvFormatError(Exception):
    """CSV 整体无法解析（空文件、缺列等）。"""


class BatchRejected(Exception):
    """存在坏行，整批退回。errors: [{"row": int, "messages": [str]}]。"""

    def __init__(self, errors):
        super().__init__(f"{len(errors)} 行数据错误，整批退回")
        self.errors = errors


@dataclass
class SegmentRec:
    tail: str
    seq: int
    dep_epoch: float
    arr_epoch: float


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _norm_seconds(x: float):
    """整秒返回 int，否则保留浮点（前端展示更干净）。"""
    return int(x) if float(x).is_integer() else x


def merge_seconds(intervals) -> float:
    """半开区间并集的总秒数。intervals: [(start, end), ...]，start < end。"""
    total = 0.0
    cur_s = cur_e = None
    for s, e in sorted(intervals):
        if cur_s is None:
            cur_s, cur_e = s, e
        elif s <= cur_e:  # 重叠或首尾相接，合并
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    if cur_s is not None:
        total += cur_e - cur_s
    return total


def day_range(date_str: str):
    """UTC 日期的半开区间 [当天00:00, 次日00:00)，返回 epoch 秒。"""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    start = datetime.combine(d, time.min, tzinfo=timezone.utc).timestamp()
    return start, start + 86400.0


def dates_spanned(dep_epoch: float, arr_epoch: float):
    """航段 [dep, arr) 覆盖到的 UTC 日期（含起飞日；恰好午夜落地时落地日不计）。"""
    first = datetime.fromtimestamp(dep_epoch, tz=timezone.utc).date()
    arr_dt = datetime.fromtimestamp(arr_epoch, tz=timezone.utc)
    last = arr_dt.date()
    # 落地恰好是午夜 00:00:00：落地瞬间不含在区间内，落地日无覆盖
    if arr_dt == datetime.combine(last, time.min, tzinfo=timezone.utc):
        last -= timedelta(days=1)
    days = []
    d = first
    while d <= last:
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# CSV 解析与行级校验（任何坏行 → BatchRejected，整批退回）
# ---------------------------------------------------------------------------

def _parse_utc_epoch(raw: str):
    """解析带时区的 ISO 8601 时间，返回 UTC epoch 秒；非法/缺时区返回 None。"""
    s = (raw or "").strip()
    if not s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc).timestamp()


def _parse_seq(raw: str):
    try:
        return int(raw)
    except (TypeError, ValueError):
        pass
    try:
        f = float(raw)
        if f.is_integer():
            return int(f)
    except (TypeError, ValueError):
        pass
    return None


def parse_batch_csv(csv_text: str) -> list:
    """解析批次 CSV，返回 [SegmentRec]；只有表头时返回空列表（撤批语义）。

    - 结构问题（空文件、缺列）→ CsvFormatError；
    - 任意坏行 → BatchRejected（汇总全部坏行后整批退回）。
    """
    reader = csv.reader(io.StringIO(csv_text))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise CsvFormatError("CSV 内容为空")
    header = [c.strip().lower().lstrip("﻿") for c in rows[0]]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise CsvFormatError(
            "缺少必需列: " + ", ".join(missing)
            + "。需要的表头为: " + ", ".join(REQUIRED_COLUMNS)
        )
    idx = {name: header.index(name) for name in REQUIRED_COLUMNS}

    def get(r, name):
        j = idx[name]
        return r[j].strip() if j < len(r) else ""

    segments = []
    errors = []
    seen_keys = set()
    for i, r in enumerate(rows[1:], start=1):  # 数据行号（表头为第 0 行）
        msgs = []
        tail = get(r, "tail_number")
        seq = _parse_seq(get(r, "seq"))
        dep = _parse_utc_epoch(get(r, "dep_utc"))
        arr = _parse_utc_epoch(get(r, "arr_utc"))
        dur_raw = get(r, "duration_minutes")

        if not tail:
            msgs.append("tail_number（机尾号）为空")
        if seq is None:
            msgs.append(f"seq 不是整数: {get(r, 'seq')!r}")
        if dep is None:
            msgs.append(
                f"dep_utc 无法解析为带时区的 ISO 8601 时间: {get(r, 'dep_utc')!r}")
        if arr is None:
            msgs.append(
                f"arr_utc 无法解析为带时区的 ISO 8601 时间: {get(r, 'arr_utc')!r}")
        if dep is not None and arr is not None and not arr > dep:
            msgs.append("arr_utc 必须晚于 dep_utc（航段时长须为正）")
        try:
            dur = float(dur_raw)
            if not (math.isfinite(dur) and dur >= 0):
                msgs.append(f"duration_minutes 不能为负: {dur_raw!r}")
        except ValueError:
            msgs.append(f"duration_minutes 不是数字: {dur_raw!r}")
        if tail and seq is not None:
            key = (tail, seq)
            if key in seen_keys:
                msgs.append(f"同一批内 tail_number+seq 重复: {tail} seq={seq}")
            seen_keys.add(key)

        if msgs:
            errors.append({"row": i, "messages": msgs})
        else:
            segments.append(SegmentRec(tail, seq, dep, arr))

    if errors:
        raise BatchRejected(errors)
    return segments


# ---------------------------------------------------------------------------
# SQLite：连接、初始化
# ---------------------------------------------------------------------------

def connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout = 10000")  # 写锁竞争时等待而非立即报错
    return conn


def init_db(db_path) -> None:
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")  # 读写并发；写事务仍互斥
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 覆盖汇总：只对受影响的 (机尾, 日期) 重算
# ---------------------------------------------------------------------------

def _compute_day_coverage(conn, tail: str, date_str: str) -> float:
    """从 segments 表现算某机尾某 UTC 日的覆盖秒数（所有批次区间并集）。"""
    day_start, day_end = day_range(date_str)
    rows = conn.execute(
        "SELECT dep_utc, arr_utc FROM segments "
        "WHERE tail_number = ? AND dep_utc < ? AND arr_utc > ?",
        (tail, day_end, day_start),
    ).fetchall()
    pieces = [(max(dep, day_start), min(arr, day_end)) for dep, arr in rows]
    return merge_seconds(pieces)


def apply_batch(db_path, batch_id: str, segments: list) -> dict:
    """换批/撤批主入口：单事务内替换航段、重算受影响汇总并提交。

    segments 为空列表 → 撤回该批（不存在则为幂等空操作）。
    """
    batch_id = (batch_id or "").strip()
    if not batch_id:
        raise ValueError("批号不能为空")
    if len(batch_id) > MAX_BATCH_ID_LEN:
        raise ValueError(f"批号过长（>{MAX_BATCH_ID_LEN} 字符）")

    conn = connect(db_path)
    try:
        # BEGIN IMMEDIATE：立即取写锁，事务彼此互斥 → 并发提交等价于串行
        conn.execute("BEGIN IMMEDIATE")

        old_rows = conn.execute(
            "SELECT tail_number, dep_utc, arr_utc FROM segments WHERE batch_id = ?",
            (batch_id,),
        ).fetchall()
        had_old = conn.execute(
            "SELECT 1 FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()

        affected = set()
        for tail, dep, arr in old_rows:
            affected.update((tail, d) for d in dates_spanned(dep, arr))

        conn.execute("DELETE FROM segments WHERE batch_id = ?", (batch_id,))
        if segments:
            conn.executemany(
                "INSERT INTO segments (batch_id, tail_number, seq, dep_utc, arr_utc) "
                "VALUES (?, ?, ?, ?, ?)",
                [(batch_id, s.tail, s.seq, s.dep_epoch, s.arr_epoch)
                 for s in segments],
            )
            conn.execute(
                "INSERT INTO batches (batch_id, uploaded_at, row_count) VALUES (?, ?, ?) "
                "ON CONFLICT(batch_id) DO UPDATE SET "
                "uploaded_at = excluded.uploaded_at, row_count = excluded.row_count",
                (batch_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 len(segments)),
            )
            for s in segments:
                affected.update((s.tail, d) for d in dates_spanned(s.dep_epoch, s.arr_epoch))
            action = "replaced" if had_old else "loaded"
        else:
            conn.execute("DELETE FROM batches WHERE batch_id = ?", (batch_id,))
            action = "withdrawn" if had_old else "withdraw_noop"

        # 只重算旧批/新批覆盖到的 (机尾, 日期)
        affected_out = []
        for tail, d in sorted(affected):
            seconds = _compute_day_coverage(conn, tail, d)
            if seconds > 0:
                conn.execute(
                    "INSERT INTO coverage (tail_number, utc_date, covered_seconds) "
                    "VALUES (?, ?, ?) ON CONFLICT(tail_number, utc_date) DO UPDATE SET "
                    "covered_seconds = excluded.covered_seconds",
                    (tail, d, seconds),
                )
            else:
                conn.execute(
                    "DELETE FROM coverage WHERE tail_number = ? AND utc_date = ?",
                    (tail, d),
                )
            affected_out.append({"tail_number": tail, "utc_date": d,
                                 "covered_seconds": _norm_seconds(seconds)})

        conn.commit()  # 批次与汇总在同一事务提交
        return {"batch_id": batch_id, "action": action,
                "rows": len(segments), "affected": affected_out}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def query_coverage(db_path, tail: str, date_str: str):
    """按机尾 + UTC 日期查覆盖秒数；无覆盖返回 0。"""
    tail = (tail or "").strip()
    if not tail:
        raise ValueError("机尾号不能为空")
    try:
        date.fromisoformat(date_str)
        datetime.strptime(date_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError(f"日期格式须为 YYYY-MM-DD: {date_str!r}") from None
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT covered_seconds FROM coverage "
            "WHERE tail_number = ? AND utc_date = ?",
            (tail, date_str),
        ).fetchone()
    finally:
        conn.close()
    seconds = row[0] if row else 0
    return {"tail_number": tail, "utc_date": date_str,
            "covered_seconds": _norm_seconds(seconds)}


def list_batches(db_path) -> list:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT batch_id, uploaded_at, row_count FROM batches ORDER BY batch_id"
        ).fetchall()
    finally:
        conn.close()
    return [{"batch_id": b, "uploaded_at": t, "row_count": n} for b, t, n in rows]


def list_coverage(db_path, tail: str = None) -> list:
    sql = ("SELECT tail_number, utc_date, covered_seconds FROM coverage")
    args = ()
    if tail:
        sql += " WHERE tail_number = ?"
        args = (tail.strip(),)
    sql += " ORDER BY tail_number, utc_date"
    conn = connect(db_path)
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return [{"tail_number": t, "utc_date": d, "covered_seconds": _norm_seconds(s)}
            for t, d, s in rows]
