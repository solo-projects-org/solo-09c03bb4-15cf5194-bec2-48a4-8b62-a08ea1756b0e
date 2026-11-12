# 航段时间清洗 · DST 安全入仓 · 月末利用率核对

面向航司数据工程师的网页工具：

1. **时间清洗**：把地服导出的**当地时间**航段链解析为唯一 **UTC** 时间链，避免夏令时（DST）切换时被错误入仓；
2. **月末利用率核对**：清洗导出的 UTC 航段 CSV 按**批号**入仓，同一航段报在多个批次也不重复计，
   按机尾 × UTC 日期汇总覆盖秒数，供机务数据员月末核对机队利用率。

## 快速启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000> ：

- 上半页：点击「📦 载入内置夏令时样例」→「开始清洗」，演示 DST 清洗；
- 下半页「月末机队利用率核对」：依次点击演示脚本 ①②③④，
  演示**上传批次 → 跨批重叠去重 → 换批 → 仅表头撤批**，并用查询框按机尾 + UTC 日期查覆盖秒数。

本地开发（不用 Docker）：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

自检（仅标准库）：`python3 tests/test_solver.py && python3 tests/test_utilization.py`

## 输入 CSV 格式（清洗）

UTF-8 编码，必需表头：

```csv
tail_number,seq,dep_local,dep_tz,arr_local,arr_tz,duration_min_minutes,duration_max_minutes
B-7001,1,2026-10-25 00:30,Europe/London,2026-10-25 02:15,Europe/Berlin,90,130
```

| 列 | 含义 |
| --- | --- |
| `tail_number` | 机尾号（同一机尾构成一条航段链） |
| `seq` | 航段顺序，整数，同一机尾内唯一，按升序链接 |
| `dep_local` / `arr_local` | 起飞/落地当地时间，`YYYY-MM-DD HH:MM`（也接受 `T` 分隔与秒） |
| `dep_tz` / `arr_tz` | 两端 IANA 时区名，如 `Europe/Berlin`、`America/New_York` |
| `duration_min_minutes` / `duration_max_minutes` | 飞行时长下/上限（分钟，**闭区间**） |

## 求解规则

1. 当地时间 → UTC 候选：
   - 普通时刻 → 1 个候选；
   - **重复时刻**（秋季拨回重叠，如柏林 2026-10-25 02:30）→ 保留 **2 个候选**参与整链求解；
   - **不存在的时刻**（春季拨快空洞，如柏林 2026-03-29 02:30）→ 该行报错，整架机尾禁止导出。
2. 每个航段的 (起飞, 落地) 候选组合必须满足：飞行时长 ∈ `[下限, 上限]`（闭区间）。
3. 按 `seq` 链接同一机尾：下一段起飞 **不得早于** 上一段落地（相等允许）。
4. 判定：
   - **整链唯一解** → 允许导出该机尾的 UTC CSV；
   - **无解** → 展示冲突航段（前向扫描定位第一个无法衔接的航段及其候选），禁止导出；
   - **多解** → 展示全部可行候选链（最多展示 20 条），禁止导出；
   - **行级错误**（不存在时刻 / 非法时区 / 格式错误 / seq 重复等）→ 禁止导出。

## 导出

- 网页上按机尾导出，或「导出全部可导出机尾」；
- 导出列：`tail_number,seq,dep_utc,arr_utc,duration_minutes`（UTC 为 ISO 8601 `Z` 格式）；
- 服务端在导出时会**重新求解并复核唯一性**，非唯一解返回 409，杜绝误导出。

## 内置样例（samples/dst_sample.csv）

| 机尾 | 场景 | 期望结果 |
| --- | --- | --- |
| B-7001 | 欧洲拨回日三连段，重复时刻由时长区间+链约束消歧 | 唯一解，可导出 |
| B-7002 | 柏林 2026-03-29 02:30 不存在（拨快空洞） | 数据错误 |
| B-7003 | 第二段起飞早于第一段落地 | 无解，展示冲突航段 |
| B-7004 | 起降均为重复时刻，区间宽松 | 多解（2 条候选链） |
| B-7005 | 美国拨回日（纽约/洛杉矶/丹佛） | 唯一解，可导出 |

## 月末利用率核对（批次入仓）

清洗导出的 UTC CSV（`tail_number,seq,dep_utc,arr_utc,duration_minutes`）可直接按批号上传入仓：

1. **换批 / 撤批**：同一批号重传 → 新文件**整体替换**旧批；文件只有表头 → **撤回**该批
   （批号不存在时撤批为幂等空操作）。
2. **坏行整批退回**：任一行非法（时间无法解析、落地不晚于起飞、seq 重复等）→ 整批拒绝，
   数据库不产生任何变更，返回全部坏行明细。
3. **跨日拆分**：航段按 UTC 午夜拆分，区间**含起飞、不含落地** `[dep, arr)` ——
   恰好 00:00 落地的航段在落地日计 0 秒，恰好 00:00 起飞的航段计入起飞当日。
4. **重叠只计一次**：同一机尾同一 UTC 日期，所有批次的航段时段取**区间并集**求和。
5. **增量重算**：每次提交只重算旧批/新批覆盖到的 (机尾, 日期) 组合；
   批次表与汇总表在**同一 SQLite 事务**中提交（`BEGIN IMMEDIATE`），
   并发换批的结果等同于逐批串行提交（写事务互斥，后提交者胜出）。
6. **查询**：按机尾 + UTC 日期（`YYYY-MM-DD`）返回覆盖秒数，无覆盖返回 `0`。

SQLite 默认落在 `data/utilization.db`（Docker 中为 `/app/data`，compose 已挂卷
`utilization-data` 持久化），可用环境变量 `UTILIZATION_DB_PATH` 覆盖。

### 利用率演示样例（samples/）

| 文件 | 内容 | 演示点 |
| --- | --- | --- |
| `coverage_batch_a.csv` | B-7001 跨日航段 23:30→01:15 + 当日航段；B-7002 当日航段 | ① 上传：跨日拆分 |
| `coverage_batch_b.csv` | 重复报送 A 的跨日航段 + 重叠航段 07:00→09:00 + B-7003 跨日 | ② 上传：重复不双计、重叠并集 |
| `coverage_batch_a_v2.csv` | 批次 A 的修订版 | ③ 换批：整批替换 |
| （网页生成仅表头文件） | 只有表头 | ④ 撤批：整批撤回 |

页面「入仓规则与演示预期」折叠区列出了每一步后的预期覆盖秒数，可对照汇总表核对。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 网页入口（清洗 + 利用率核对同页） |
| `GET` | `/api/sample` | 下载清洗演示样例 CSV |
| `POST` | `/api/solve` | 上传 CSV（multipart 字段 `file`），返回逐机尾求解结果 JSON |
| `POST` | `/api/export` | 字段 `file` + `tail`（`__all__` 表示全部可导出机尾），返回 UTC CSV；非唯一解 409 |
| `POST` | `/api/batches` | 字段 `batch_id` + `file`：批次入仓/换批/（仅表头）撤批；坏行 400 整批退回 |
| `GET` | `/api/batches` | 已入仓批次列表 |
| `GET` | `/api/coverage?tail=…&date=YYYY-MM-DD` | 该机尾该 UTC 日的覆盖秒数（无覆盖为 0） |
| `GET` | `/api/coverage/table` | 全量覆盖汇总表（可选 `tail` 过滤） |
| `GET` | `/api/utilization-sample/{a\|b\|a2}` | 利用率演示样例 CSV |
| `GET` | `/docs` | FastAPI 自动文档 |

## 目录结构

```
app/
  main.py            # FastAPI 路由（清洗 + 批次入仓 + 覆盖查询）
  solver.py          # 候选生成 + 整链求解 + 冲突定位（纯标准库）
  utilization.py     # 批次换/撤 + 跨日拆分 + 重叠并集 + SQLite 事务（纯标准库）
  static/index.html  # 单页前端
samples/dst_sample.csv             # 清洗演示样例
samples/coverage_batch_{a,b,a2}.csv  # 利用率演示样例
tests/test_solver.py               # 清洗自检（仅标准库）
tests/test_utilization.py          # 利用率自检：拆分/并集/换撤批/并发等价（仅标准库）
Dockerfile / docker-compose.yml / requirements.txt
```
