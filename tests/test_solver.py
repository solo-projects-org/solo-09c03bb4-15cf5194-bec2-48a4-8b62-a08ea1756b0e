"""求解器自检：直接运行 `python3 tests/test_solver.py`（仅依赖标准库 + tzdata）。"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.solver import (  # noqa: E402
    build_export_csv, solve_csv, utc_candidates,
)

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "dst_sample.csv"


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        raise SystemExit(1)


def main():
    # 1) 时区候选映射
    cands, err = utc_candidates(datetime(2026, 10, 25, 2, 30), "Europe/Berlin")
    check("柏林拨回：重复时刻保留两个候选", err is None and len(cands) == 2)

    cands, err = utc_candidates(datetime(2026, 3, 29, 2, 30), "Europe/Berlin")
    check("柏林拨快：不存在时刻报错", err == "nonexistent")

    cands, err = utc_candidates(datetime(2026, 6, 1, 12, 0), "Europe/Berlin")
    check("普通时刻：唯一候选", err is None and len(cands) == 1)

    cands, err = utc_candidates(datetime(2026, 1, 1, 0, 0), "Mars/Olympus")
    check("非法 IANA 时区", err == "unknown_tz")

    # 2) 样例整体求解
    result = solve_csv(SAMPLE.read_text(encoding="utf-8"))
    by_tail = {t["tail"]: t for t in result["tails"]}

    check("B-7001 整链唯一解", by_tail["B-7001"]["status"] == "unique")
    check("B-7002 不存在时刻 → error", by_tail["B-7002"]["status"] == "error")
    check("B-7003 链断裂 → no_solution", by_tail["B-7003"]["status"] == "no_solution")
    check("B-7003 冲突定位在 seq=2",
          by_tail["B-7003"]["conflict"]["segment_seq"] == 2)
    check("B-7004 多解", by_tail["B-7004"]["status"] == "multiple")
    check("B-7004 恰好 2 条候选链", by_tail["B-7004"]["solution_count"] == 2)
    check("B-7005 整链唯一解", by_tail["B-7005"]["status"] == "unique")

    # 3) 唯一解内容校验（B-7001 第一段：伦敦 00:30 BST → 23:30Z；
    #    柏林 02:15 双候选 00:15Z/01:15Z，时长 45/165 分钟，区间 [90,130] 只留下 01:15Z）
    sol1 = by_tail["B-7001"]["solution"][0]
    check("B-7001 seg1 起飞 UTC", sol1["dep_utc"] == "2026-10-24T23:30:00Z")
    check("B-7001 seg1 落地 UTC", sol1["arr_utc"] == "2026-10-25T01:15:00Z")

    # 4) 导出只包含唯一解机尾
    csv_out = build_export_csv(result, ["B-7001", "B-7004", "B-7005"])
    lines = [l for l in csv_out.strip().splitlines()]
    check("导出 CSV 行数 = 表头 + B-7001(3) + B-7005(2)", len(lines) == 1 + 3 + 2)
    check("导出 CSV 不含多解机尾 B-7004", "B-7004" not in csv_out)

    print("\n全部自检通过 ✅")


if __name__ == "__main__":
    main()
