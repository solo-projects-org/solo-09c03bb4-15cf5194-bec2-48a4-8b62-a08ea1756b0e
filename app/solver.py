"""航段链 UTC 求解核心逻辑。

业务规则
--------
- 每行航段给出起飞/落地的当地时间与两端 IANA 时区；
- 当地时间在该时区中不存在（春季拨快产生的空洞）→ 该行报错，整架机尾禁止导出；
- 当地时间重复（秋季拨回产生的重叠）→ 保留两个 UTC 候选，参与整链求解；
- 飞行时长（落地 - 起飞）必须落在闭区间 [duration_min_minutes, duration_max_minutes]；
- 同一机尾按 seq 升序链接：下一段起飞不得早于上一段落地（相等允许）；
- 整链恰好唯一解才允许导出；无解展示冲突航段，多解展示可行候选。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REQUIRED_COLUMNS = [
    "tail_number",
    "seq",
    "dep_local",
    "dep_tz",
    "arr_local",
    "arr_tz",
    "duration_min_minutes",
    "duration_max_minutes",
]

_DT_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)

# 枚举整链解时的硬上限（每条链至多 4^N 条路径，实际远小于此）
SOLUTION_CAP = 200
# 多解时返回给前端展示的候选条数
MAX_DISPLAY_SOLUTIONS = 20


class CsvFormatError(Exception):
    """CSV 整体无法解析（缺列、空文件等）。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_duration(minutes: float):
    return int(minutes) if float(minutes).is_integer() else round(minutes, 1)


def parse_naive(text: str):
    """解析当地时间字符串为 naive datetime，失败返回 None。"""
    text = (text or "").strip()
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def utc_candidates(naive: datetime, tz_name: str):
    """把 naive 当地时间映射为可能的 UTC 时刻。

    返回 (sorted_candidates, error_kind)：
    - error_kind 为 None：候选列表（重复时刻含 2 个候选，普通时刻 1 个）
    - error_kind 为 "unknown_tz" / "nonexistent"：候选为 None
    """
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None, "unknown_tz"
    found = set()
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        utc = aware.astimezone(timezone.utc)
        # 回代校验：只有能还原成本地时间的偏移才是合法候选
        if utc.astimezone(tz).replace(tzinfo=None) == naive:
            found.add(utc)
    if not found:
        return None, "nonexistent"
    return sorted(found), None


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class RawRow:
    row_number: int  # 数据行号（表头为第 0 行，数据从 1 起）
    tail: str
    seq_raw: str
    dep_local_raw: str
    dep_tz: str
    arr_local_raw: str
    arr_tz: str
    dur_min_raw: str
    dur_max_raw: str


@dataclass
class Combo:
    dep_utc: datetime
    arr_utc: datetime

    @property
    def duration_minutes(self) -> float:
        return (self.arr_utc - self.dep_utc).total_seconds() / 60.0


@dataclass
class Segment:
    row_number: int
    seq: int
    dep_local_raw: str
    dep_tz: str
    arr_local_raw: str
    arr_tz: str
    dur_min: int
    dur_max: int
    dep_candidates: list = field(default_factory=list)  # list[datetime]
    arr_candidates: list = field(default_factory=list)
    combos: list = field(default_factory=list)  # 满足时长区间的 Combo


# ---------------------------------------------------------------------------
# CSV 读取与行级校验
# ---------------------------------------------------------------------------

def read_rows(csv_text: str) -> list:
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

    out = []
    for i, r in enumerate(rows[1:], start=1):
        out.append(RawRow(
            row_number=i,
            tail=get(r, "tail_number"),
            seq_raw=get(r, "seq"),
            dep_local_raw=get(r, "dep_local"),
            dep_tz=get(r, "dep_tz"),
            arr_local_raw=get(r, "arr_local"),
            arr_tz=get(r, "arr_tz"),
            dur_min_raw=get(r, "duration_min_minutes"),
            dur_max_raw=get(r, "duration_max_minutes"),
        ))
    if not out:
        raise CsvFormatError("CSV 没有任何数据行")
    return out


def _parse_minutes(raw: str):
    """解析分钟数，容忍 '420' 与 '420.0' 两种写法。"""
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        f = float(raw)
        if f.is_integer():
            return int(f)
    except ValueError:
        pass
    return None


def build_segment(raw: RawRow):
    """行级校验 + 候选生成。返回 (Segment|None, [错误信息])。"""
    errors = []

    if not raw.tail:
        errors.append("tail_number（机尾号）为空")

    try:
        seq = int(raw.seq_raw)
    except ValueError:
        errors.append(f"seq 不是整数: {raw.seq_raw!r}")
        seq = None

    dur_min = _parse_minutes(raw.dur_min_raw)
    dur_max = _parse_minutes(raw.dur_max_raw)
    if dur_min is None:
        errors.append(f"duration_min_minutes 不是整数分钟: {raw.dur_min_raw!r}")
    if dur_max is None:
        errors.append(f"duration_max_minutes 不是整数分钟: {raw.dur_max_raw!r}")
    if dur_min is not None and dur_max is not None:
        if dur_min < 0:
            errors.append(f"duration_min_minutes 不能为负: {dur_min}")
        if dur_min > dur_max:
            errors.append(f"飞行时长区间非法: 下限 {dur_min} 大于上限 {dur_max}")

    dep_naive = parse_naive(raw.dep_local_raw)
    if dep_naive is None:
        errors.append(
            f"起飞当地时间无法解析: {raw.dep_local_raw!r}（支持格式 YYYY-MM-DD HH:MM）")
    arr_naive = parse_naive(raw.arr_local_raw)
    if arr_naive is None:
        errors.append(
            f"落地当地时间无法解析: {raw.arr_local_raw!r}（支持格式 YYYY-MM-DD HH:MM）")

    dep_candidates = arr_candidates = None
    if dep_naive is not None:
        dep_candidates, err = utc_candidates(dep_naive, raw.dep_tz)
        if err == "unknown_tz":
            errors.append(f"起飞时区不是合法的 IANA 时区: {raw.dep_tz!r}")
        elif err == "nonexistent":
            errors.append(
                f"起飞当地时间 {raw.dep_local_raw} 在时区 {raw.dep_tz} 中不存在"
                "（夏令时拨快跳过了该时刻），请与地服核对")
    if arr_naive is not None:
        arr_candidates, err = utc_candidates(arr_naive, raw.arr_tz)
        if err == "unknown_tz":
            errors.append(f"落地时区不是合法的 IANA 时区: {raw.arr_tz!r}")
        elif err == "nonexistent":
            errors.append(
                f"落地当地时间 {raw.arr_local_raw} 在时区 {raw.arr_tz} 中不存在"
                "（夏令时拨快跳过了该时刻），请与地服核对")

    if errors:
        return None, errors

    seg = Segment(
        row_number=raw.row_number, seq=seq,
        dep_local_raw=raw.dep_local_raw, dep_tz=raw.dep_tz,
        arr_local_raw=raw.arr_local_raw, arr_tz=raw.arr_tz,
        dur_min=dur_min, dur_max=dur_max,
        dep_candidates=dep_candidates, arr_candidates=arr_candidates,
    )
    for d in dep_candidates:
        for a in arr_candidates:
            combo = Combo(d, a)
            if dur_min <= combo.duration_minutes <= dur_max:
                seg.combos.append(combo)
    return seg, []


# ---------------------------------------------------------------------------
# 整链求解
# ---------------------------------------------------------------------------

def enumerate_solutions(segments: list):
    """DFS 枚举整条航段链的全部可行解，返回 (solutions, truncated)。"""
    solutions = []
    truncated = False
    path = []

    def dfs(i: int, prev_arr):
        nonlocal truncated
        if truncated:
            return
        if i == len(segments):
            solutions.append(list(path))
            if len(solutions) >= SOLUTION_CAP:
                truncated = True
            return
        for combo in segments[i].combos:
            if prev_arr is None or combo.dep_utc >= prev_arr:
                path.append(combo)
                dfs(i + 1, combo.arr_utc)
                path.pop()
                if truncated:
                    return

    dfs(0, None)
    return solutions, truncated


def analyze_conflict(segments: list) -> dict:
    """无解时定位冲突：前向扫描，找到第一个无法衔接的航段。"""
    prev_arrs = [None]  # None 表示链首，无约束
    for seg in segments:
        if not seg.combos:
            return {
                "type": "duration_window",
                "segment_seq": seg.seq,
                "row": seg.row_number,
                "message": (
                    f"航段 seq={seg.seq}（第 {seg.row_number} 行）没有任何 "
                    f"(起飞, 落地) 候选组合满足飞行时长闭区间 "
                    f"[{seg.dur_min}, {seg.dur_max}] 分钟"),
                "dep_candidates": [iso(d) for d in seg.dep_candidates],
                "arr_candidates": [iso(a) for a in seg.arr_candidates],
                "previous_arrival_candidates": [],
            }
        reachable = [c for c in seg.combos
                     if any(pa is None or c.dep_utc >= pa for pa in prev_arrs)]
        if not reachable:
            earliest_dep = min(c.dep_utc for c in seg.combos)
            latest_prev = max(pa for pa in prev_arrs if pa is not None)
            return {
                "type": "chain_order",
                "segment_seq": seg.seq,
                "row": seg.row_number,
                "message": (
                    f"航段 seq={seg.seq}（第 {seg.row_number} 行）无法衔接："
                    f"其最早可行起飞 {iso(earliest_dep)} 早于上一段最晚落地 "
                    f"{iso(latest_prev)}"),
                "dep_candidates": sorted({iso(c.dep_utc) for c in seg.combos}),
                "arr_candidates": sorted({iso(c.arr_utc) for c in seg.combos}),
                "previous_arrival_candidates": [iso(pa) for pa in prev_arrs if pa],
            }
        prev_arrs = sorted({c.arr_utc for c in reachable})
    # 理论上不会走到这里（前向可达非空 ⇔ 存在完整解）
    return {"type": "unknown", "message": "未能定位冲突", "segment_seq": None,
            "row": None, "dep_candidates": [], "arr_candidates": [],
            "previous_arrival_candidates": []}


# ---------------------------------------------------------------------------
# 视图与入口
# ---------------------------------------------------------------------------

def _segment_view(seg: Segment) -> dict:
    return {
        "row": seg.row_number,
        "seq": seg.seq,
        "dep_local": seg.dep_local_raw,
        "dep_tz": seg.dep_tz,
        "arr_local": seg.arr_local_raw,
        "arr_tz": seg.arr_tz,
        "dur_min": seg.dur_min,
        "dur_max": seg.dur_max,
        "dep_candidates": [iso(d) for d in seg.dep_candidates],
        "arr_candidates": [iso(a) for a in seg.arr_candidates],
        "combos": [
            {"dep_utc": iso(c.dep_utc), "arr_utc": iso(c.arr_utc),
             "duration_minutes": _fmt_duration(c.duration_minutes)}
            for c in seg.combos
        ],
    }


def _solution_view(solution, segments) -> list:
    return [
        {"seq": seg.seq, "dep_utc": iso(c.dep_utc), "arr_utc": iso(c.arr_utc),
         "duration_minutes": _fmt_duration(c.duration_minutes)}
        for seg, c in zip(segments, solution)
    ]


def solve_tail(tail: str, raws: list) -> dict:
    errors = []
    segments = []
    for raw in raws:
        seg, errs = build_segment(raw)
        if errs:
            errors.append({"row": raw.row_number, "messages": errs})
        else:
            segments.append(seg)
    if errors:
        return {"tail": tail, "status": "error", "errors": errors}

    seqs = [s.seq for s in segments]
    if len(set(seqs)) != len(seqs):
        dup = sorted({s for s in seqs if seqs.count(s) > 1})
        return {"tail": tail, "status": "error", "errors": [
            {"row": None, "messages": [f"机尾 {tail} 的航段顺序 seq 重复: {dup}"]}]}

    segments.sort(key=lambda s: s.seq)
    base = {"tail": tail, "segments": [_segment_view(s) for s in segments]}

    solutions, truncated = enumerate_solutions(segments)
    if len(solutions) == 1:
        return {**base, "status": "unique",
                "solution": _solution_view(solutions[0], segments)}
    if not solutions:
        return {**base, "status": "no_solution",
                "conflict": analyze_conflict(segments)}
    return {
        **base,
        "status": "multiple",
        "solution_count": len(solutions),
        "solution_count_truncated": truncated,
        "solutions": [_solution_view(s, segments)
                      for s in solutions[:MAX_DISPLAY_SOLUTIONS]],
    }


def solve_csv(csv_text: str) -> dict:
    """主入口：解析 CSV 文本，按机尾分组求解，返回完整结果 dict。"""
    raws = read_rows(csv_text)
    tails: dict = {}
    for raw in raws:
        tails.setdefault(raw.tail, []).append(raw)

    results = [solve_tail(tail, group) for tail, group in tails.items()]
    summary = {
        "rows": len(raws),
        "total_tails": len(results),
        "unique": sum(1 for r in results if r["status"] == "unique"),
        "no_solution": sum(1 for r in results if r["status"] == "no_solution"),
        "multiple": sum(1 for r in results if r["status"] == "multiple"),
        "error": sum(1 for r in results if r["status"] == "error"),
    }
    return {"summary": summary, "tails": results}


def build_export_csv(solve_result: dict, tails: list) -> str:
    """为已确认唯一解的机尾生成 UTC CSV（列：机尾、顺序、起降 UTC、时长）。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["tail_number", "seq", "dep_utc", "arr_utc", "duration_minutes"])
    wanted = set(tails)
    for t in solve_result["tails"]:
        if t["tail"] in wanted and t["status"] == "unique":
            for item in t["solution"]:
                writer.writerow([t["tail"], item["seq"], item["dep_utc"],
                                 item["arr_utc"], item["duration_minutes"]])
    return buf.getvalue()
