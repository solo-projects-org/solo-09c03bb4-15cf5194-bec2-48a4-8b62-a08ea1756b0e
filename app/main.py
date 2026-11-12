"""FastAPI 入口：航段时间清洗（DST 安全入仓）+ 月末机队利用率核对。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response

from . import utilization
from .solver import CsvFormatError, build_export_csv, solve_csv

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_PATH = BASE_DIR.parent / "samples" / "dst_sample.csv"
INDEX_PATH = BASE_DIR / "static" / "index.html"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

# SQLite 位置：Docker 中挂卷到 /app/data 持久化；可用环境变量覆盖
DB_PATH = os.environ.get(
    "UTILIZATION_DB_PATH", str(BASE_DIR.parent / "data" / "utilization.db"))

UTIL_SAMPLES = {
    "a": "coverage_batch_a.csv",
    "b": "coverage_batch_b.csv",
    "a2": "coverage_batch_a_v2.csv",
}


@asynccontextmanager
async def lifespan(_app):
    utilization.init_db(DB_PATH)
    yield


app = FastAPI(title="航段时间清洗 · DST 安全入仓 · 利用率核对",
              version="2.0.0", lifespan=lifespan)


async def _read_csv_text(file: UploadFile) -> str:
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件超过 10 MB 上限")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "文件不是合法的 UTF-8 编码 CSV")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(INDEX_PATH)


@app.get("/api/sample", include_in_schema=False)
async def sample():
    """内置夏令时样例，可直接下载或在前端一键载入。"""
    return FileResponse(SAMPLE_PATH, media_type="text/csv; charset=utf-8",
                        filename="dst_sample.csv")


@app.post("/api/solve")
async def solve(file: UploadFile = File(...)):
    """上传 CSV，返回每个机尾的求解结果（唯一解/无解/多解/错误）。"""
    text = await _read_csv_text(file)
    try:
        return solve_csv(text)
    except CsvFormatError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/export")
async def export(file: UploadFile = File(...), tail: str = Form(...)):
    """导出机尾的 UTC CSV。仅当整链唯一解时允许；tail=__all__ 导出全部可导出机尾。"""
    text = await _read_csv_text(file)
    try:
        result = solve_csv(text)
    except CsvFormatError as exc:
        raise HTTPException(400, str(exc))

    if tail == "__all__":
        tails = [t["tail"] for t in result["tails"] if t["status"] == "unique"]
        if not tails:
            raise HTTPException(409, "没有可导出的机尾：仅整链唯一解的机尾允许导出")
        filename = "utc_export_all.csv"
    else:
        match = next((t for t in result["tails"] if t["tail"] == tail), None)
        if match is None:
            raise HTTPException(404, f"CSV 中不存在机尾 {tail!r}")
        if match["status"] != "unique":
            raise HTTPException(
                409, f"机尾 {tail} 状态为 {match['status']}，非整链唯一解，禁止导出")
        tails = [tail]
        filename = f"utc_export_{tail}.csv"

    csv_text = build_export_csv(result, tails)
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 月末机队利用率核对：批次入仓 / 覆盖秒数查询（SQLite 事务保证）
# ---------------------------------------------------------------------------

def _decode_csv(data: bytes) -> str:
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件超过 10 MB 上限")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "文件不是合法的 UTF-8 编码 CSV")


@app.post("/api/batches")
def upload_batch(batch_id: str = Form(...), file: UploadFile = File(...)):
    """上传一批 UTC 航段 CSV。同批号重传 → 整批替换；只有表头 → 撤回该批。

    任何坏行 → 整批退回（400），数据库不变。批次与汇总在同一事务提交。
    """
    text = _decode_csv(file.file.read())
    try:
        segments = utilization.parse_batch_csv(text)
    except utilization.CsvFormatError as exc:
        raise HTTPException(400, str(exc))
    except utilization.BatchRejected as exc:
        raise HTTPException(400, detail={
            "message": f"批次 {batch_id!r} 存在坏行，已整批退回，未写入任何数据",
            "errors": exc.errors,
        })
    try:
        return utilization.apply_batch(DB_PATH, batch_id, segments)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/batches")
def batches():
    """当前已入仓的批次列表。"""
    return {"batches": utilization.list_batches(DB_PATH)}


@app.get("/api/coverage")
def coverage(tail: str = Query(...), date: str = Query(...)):
    """按机尾 + UTC 日期（YYYY-MM-DD）查覆盖秒数；无覆盖返回 0。"""
    try:
        return utilization.query_coverage(DB_PATH, tail, date)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/coverage/table")
def coverage_table(tail: str | None = None):
    """全量（或按机尾过滤的）覆盖汇总表，供月末核对页面展示。"""
    return {"rows": utilization.list_coverage(DB_PATH, tail)}


@app.get("/api/utilization-sample/{name}", include_in_schema=False)
async def utilization_sample(name: str):
    """利用率核对演示样例（a / b / a2）。"""
    filename = UTIL_SAMPLES.get(name)
    if filename is None:
        raise HTTPException(404, f"未知样例: {name!r}")
    return FileResponse(BASE_DIR.parent / "samples" / filename,
                        media_type="text/csv; charset=utf-8", filename=filename)
