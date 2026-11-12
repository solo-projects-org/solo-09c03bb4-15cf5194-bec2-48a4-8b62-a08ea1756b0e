"""利用率核对自检：直接运行 `python3 tests/test_utilization.py`（仅标准库）。"""
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import utilization as U  # noqa: E402

HEADER = "tail_number,seq,dep_utc,arr_utc,duration_minutes\n"

BATCH_A = (HEADER
           + "B-7001,1,2026-10-24T23:30:00Z,2026-10-25T01:15:00Z,105\n"
           + "B-7001,2,2026-10-25T06:00:00Z,2026-10-25T08:00:00Z,120\n"
           + "B-7002,1,2026-10-25T10:00:00Z,2026-10-25T12:30:00Z,150\n")
BATCH_B = (HEADER
           + "B-7001,1,2026-10-24T23:30:00Z,2026-10-25T01:15:00Z,105\n"
           + "B-7001,2,2026-10-25T07:00:00Z,2026-10-25T09:00:00Z,120\n"
           + "B-7003,1,2026-10-25T22:30:00Z,2026-10-26T01:30:00Z,180\n")
BATCH_A2 = (HEADER
            + "B-7001,1,2026-10-24T20:00:00Z,2026-10-24T22:30:00Z,150\n"
            + "B-7002,1,2026-10-25T10:00:00Z,2026-10-25T11:00:00Z,60\n")


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        raise SystemExit(1)


def fresh_db(tmp):
    path = Path(tmp) / "t.db"
    U.init_db(path)
    return path


def cov(db, tail, d):
    return U.query_coverage(db, tail, d)["covered_seconds"]


def recompute_all(db):
    """绕过 coverage 表，直接从 segments 表暴力重算，用于一致性校验。"""
    conn = U.connect(db)
    try:
        rows = conn.execute(
            "SELECT tail_number, dep_utc, arr_utc FROM segments").fetchall()
    finally:
        conn.close()
    by_pair = {}
    for tail, dep, arr in rows:
        for d in U.dates_spanned(dep, arr):
            s, e = U.day_range(d)
            by_pair.setdefault((tail, d), []).append((max(dep, s), min(arr, e)))
    return {k: U.merge_seconds(v) for k, v in by_pair.items()}


def coverage_table(db):
    return {(r["tail_number"], r["utc_date"]): r["covered_seconds"]
            for r in U.list_coverage(db)}


def main():
    # ------------------------------------------------------------------
    # 1) CSV 解析与行级校验
    # ------------------------------------------------------------------
    segs = U.parse_batch_csv(BATCH_A)
    check("合法文件解析出 3 条航段", len(segs) == 3)
    check("只有表头 → 空列表（撤批语义）", U.parse_batch_csv(HEADER) == [])
    check("空行被忽略", U.parse_batch_csv(HEADER + "\n\n  \n") == [])

    try:
        U.parse_batch_csv("tail_number,seq\nB-7001,1\n")
        check("缺列 → CsvFormatError", False)
    except U.CsvFormatError:
        check("缺列 → CsvFormatError", True)

    bad = (HEADER
           + "B-1,1,2026-10-24T23:30:00Z,2026-10-24T23:30:00Z,0\n"      # 落地不晚于起飞
           + "B-1,2,2026-10-24 23:30:00,2026-10-25T01:00:00Z,10\n"     # 缺时区
           + "B-1,2,2026-10-24T23:00:00Z,2026-10-25T01:00:00Z,10\n"    # seq 重复
           + "B-1,x,2026-10-24T23:00:00Z,2026-10-25T01:00:00Z,10\n"    # seq 非整数
           + "B-1,3,not-a-time,2026-10-25T01:00:00Z,abc\n")            # 时间+时长非法
    try:
        U.parse_batch_csv(bad)
        check("坏行 → BatchRejected", False)
    except U.BatchRejected as exc:
        check("坏行 → BatchRejected", True)
        check("5 个坏行全部汇总", len(exc.errors) == 5)
        check("行号从 1 起", [e["row"] for e in exc.errors] == [1, 2, 3, 4, 5])

    # ------------------------------------------------------------------
    # 2) 跨日拆分 + 含起飞不含落地
    # ------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(tmp)
        r = U.apply_batch(db, "A", U.parse_batch_csv(BATCH_A))
        check("批次A入仓 action=loaded", r["action"] == "loaded")
        check("跨日拆分：24 日 1800 秒", cov(db, "B-7001", "2026-10-24") == 1800)
        check("跨日拆分：25 日 4500+7200", cov(db, "B-7001", "2026-10-25") == 11700)
        check("无覆盖日期 → 0", cov(db, "B-7001", "2026-10-27") == 0)
        check("无机尾 → 0", cov(db, "B-9999", "2026-10-25") == 0)

        # 恰好午夜落地：落地日不计；恰好午夜起飞：计入当日
        edge = U.parse_batch_csv(
            HEADER
            + "B-8,1,2026-10-25T06:00:00Z,2026-10-26T00:00:00Z,1080\n"
            + "B-8,2,2026-10-27T00:00:00Z,2026-10-27T02:00:00Z,120\n")
        U.apply_batch(db, "EDGE", edge)
        check("午夜落地：当天计到 24:00", cov(db, "B-8", "2026-10-25") == 64800)
        check("午夜落地：落地日 0 秒", cov(db, "B-8", "2026-10-26") == 0)
        check("午夜起飞：计入起飞当日", cov(db, "B-8", "2026-10-27") == 7200)

    # ------------------------------------------------------------------
    # 3) 跨批重叠并集 + 重复报送不双计
    # ------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(tmp)
        U.apply_batch(db, "A", U.parse_batch_csv(BATCH_A))
        r = U.apply_batch(db, "B", U.parse_batch_csv(BATCH_B))
        check("批次B入仓", r["action"] == "loaded")
        check("重复航段不双计：24 日仍 1800", cov(db, "B-7001", "2026-10-24") == 1800)
        check("重叠并集：4500 + union(6-8,7-9)=10800 → 15300",
              cov(db, "B-7001", "2026-10-25") == 15300)
        check("B-7003 跨日：25 日 5400", cov(db, "B-7003", "2026-10-25") == 5400)
        check("B-7003 跨日：26 日 5400", cov(db, "B-7003", "2026-10-26") == 5400)

        # --------------------------------------------------------------
        # 4) 换批：整批替换，只重算受影响组合
        # --------------------------------------------------------------
        r = U.apply_batch(db, "A", U.parse_batch_csv(BATCH_A2))
        check("重传同批号 action=replaced", r["action"] == "replaced")
        check("换批后 24 日：A2 9000 + B 1800 = 10800",
              cov(db, "B-7001", "2026-10-24") == 10800)
        check("换批后 25 日：只剩 B 的 4500+7200",
              cov(db, "B-7001", "2026-10-25") == 11700)
        check("B-7002 被替换为 3600", cov(db, "B-7002", "2026-10-25") == 3600)
        check("B-7003 不受换批影响", cov(db, "B-7003", "2026-10-26") == 5400)
        check("coverage 表与 segments 表一致", coverage_table(db) == recompute_all(db))

        # --------------------------------------------------------------
        # 5) 撤批：仅表头 → 整批撤回；不存在的批号 → 幂等空操作
        # --------------------------------------------------------------
        r = U.apply_batch(db, "B", [])
        check("仅表头 → withdrawn", r["action"] == "withdrawn")
        check("撤批后 B-7001@25 = 0", cov(db, "B-7001", "2026-10-25") == 0)
        check("撤批后 B-7003@26 = 0", cov(db, "B-7003", "2026-10-26") == 0)
        check("撤批后 B-7001@24 = 9000（A2 保留）",
              cov(db, "B-7001", "2026-10-24") == 9000)
        r = U.apply_batch(db, "NEVER-SEEN", [])
        check("撤回不存在的批 → withdraw_noop", r["action"] == "withdraw_noop")
        check("撤批后 coverage 表仍一致", coverage_table(db) == recompute_all(db))

        # --------------------------------------------------------------
        # 6) 坏行整批退回，数据库不变
        # --------------------------------------------------------------
        before = coverage_table(db)
        try:
            U.parse_batch_csv(HEADER + "B-1,1,bad,bad,0\n")
            check("坏行整批退回", False)
        except U.BatchRejected:
            check("坏行整批退回", True)
        check("坏行提交后数据库无变化", coverage_table(db) == before)

    # ------------------------------------------------------------------
    # 7) 并发：不同批号并行 ≡ 逐批串行
    # ------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(tmp)
        files = {}
        for i in range(8):
            # 同一机尾、互相重叠的航段，批号各不相同（小时 08..15 均合法）
            day = f"2026-11-0{1 + i % 3}"
            files[f"P{i}"] = (
                HEADER
                + f"B-P,1,{day}T{8 + i:02d}:00:00Z,{day}T{9 + i:02d}:30:00Z,90\n"
                + f"B-P,2,{day}T{8 + i:02d}:15:00Z,{day}T{8 + i:02d}:45:00Z,30\n")

        errors = []

        def worker(bid, text):
            try:
                U.apply_batch(db, bid, U.parse_batch_csv(text))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(bid, text))
                   for bid, text in files.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("并行入仓无异常", not errors)

        # 参照库：同一批文件逐批串行提交
        ref_path = Path(tmp) / "ref.db"
        U.init_db(ref_path)
        for bid, text in files.items():
            U.apply_batch(ref_path, bid, U.parse_batch_csv(text))
        check("并行结果 ≡ 逐批串行", coverage_table(db) == coverage_table(ref_path))
        check("并行后 coverage 与 segments 一致",
              coverage_table(db) == recompute_all(db))

    # ------------------------------------------------------------------
    # 8) 并发：同批号并发换批 → 等价于某次串行顺序（最后提交者胜出）
    # ------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(tmp)
        v1 = (HEADER + "B-R,1,2026-11-05T10:00:00Z,2026-11-05T12:00:00Z,120\n")
        v2 = (HEADER + "B-R,1,2026-11-05T10:00:00Z,2026-11-05T13:00:00Z,180\n"
              + "B-R,2,2026-11-06T08:00:00Z,2026-11-06T09:00:00Z,60\n")
        errors = []
        barrier = threading.Barrier(2)

        def racer(text):
            try:
                for _ in range(20):
                    barrier.wait()
                    U.apply_batch(db, "R", U.parse_batch_csv(text))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=racer, args=(v1,))
        t2 = threading.Thread(target=racer, args=(v2,))
        t1.start(); t2.start(); t1.join(); t2.join()
        check("并发换批无异常", not errors)
        final = coverage_table(db)
        check("并发换批后 coverage 与 segments 一致", final == recompute_all(db))
        # 最终状态必须等于「v1 最后提交」或「v2 最后提交」二者之一
        eq_v1 = final == {("B-R", "2026-11-05"): 7200}
        eq_v2 = final == {("B-R", "2026-11-05"): 10800, ("B-R", "2026-11-06"): 3600}
        check("结果等同于某个串行顺序（最后写入者胜出）", eq_v1 or eq_v2)

    # ------------------------------------------------------------------
    # 9) 查询参数校验
    # ------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(tmp)
        for bad_date in ["2026-13-01", "25/10/2026", ""]:
            try:
                U.query_coverage(db, "B-1", bad_date)
                check(f"非法日期 {bad_date!r} 拒绝", False)
            except ValueError:
                check(f"非法日期 {bad_date!r} 拒绝", True)
        try:
            U.query_coverage(db, "  ", "2026-10-25")
            check("空机尾拒绝", False)
        except ValueError:
            check("空机尾拒绝", True)

    print("\n全部自检通过 ✅")


if __name__ == "__main__":
    main()
