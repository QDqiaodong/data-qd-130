# 双时态计划修订仓 v1（SQLite）

在不改写既有快照链路（`project_path.cli` / `model.py`）的前提下，新增
`project_path.history`：把计划变更作为不可变修订行存入本地 SQLite，支持乱序补录、
按双时态坐标重放，并复用快照模型的严格输入校验与安全报告发布。仅使用 Python 标准库
（`sqlite3`、`json`、`hashlib`、`argparse` 等）。

## 两条时间轴

| 轴 | 字段 | 含义 |
| --- | --- | --- |
| 业务时间 | `effective_at` | 修订生效时刻，非负整数，上限 10^9；允许乱序补录过去的时刻 |
| 入库时间 | `known_seq` + `seq_in_batch` | 批次接受时分配的批次序号（从 1 递增）与批内行序号 |

已接受的行只增不改不删，因此任意旧 `known_seq` 的查询结果永久可复现。

## 批次文档

与快照输入共用 `common.read`：最多 8 MB、拒绝重复 JSON 键、拒绝 `NaN`/`Infinity`，
源字节 SHA256 随批次保留。结构固定为两个字段，多字段即非法：

```json
{
  "batch_id": "b-2026-001",
  "revisions": [
    {"task_id": "C", "effective_at": 5, "op": "upsert",
     "duration": 4, "predecessors": ["A", "B"]},
    {"task_id": "E", "effective_at": 5, "op": "delete"}
  ]
}
```

* `upsert`：完整替换该任务（全量工期与全量父列表，非增量补丁）；`duration` 为
  0..10^6 整数，`predecessors` 为去重的标识列表，父列表语义与快照契约一致。
* `delete`：仅含 `task_id`、`effective_at`、`op`，表示该任务自此时刻起不存在。
* 同一批次内 `(task_id, effective_at)` 重复即整批非法；不同任务共用同一
  `effective_at` 合法，并在该时刻联合应用，不存在半应用状态。
* 批次至少含一条修订；标识与整数边界沿用 `common.ident` / `common.number`。

## 接受规则（单事务）

1. 若 `batch_id` 已存在：源字节完全相同视为重试，返回**原 `known_seq`**（状态
   `replayed`）；字节不同则报 `batch_id conflict`，不写入任何内容。
2. 在 `BEGIN IMMEDIATE` 事务内取下一个 `known_seq = MAX(known_seq)+1`，把候选行与
   既有历史合并，按 `(effective_at, known_seq, seq_in_batch)` 排序。
3. 对候选历史的**全部生效断点**逐一物化快照并调用 `model.analyze` 校验：
   * 快照为空（所有现存任务在该时刻被删除）；
   * 悬空依赖（父任务不存在，含被删除或从未写入）；
   * 依赖成环，包括只在补录区间内出现、区间外无环的中间时段环；
   * 任务/父列表重复、越界等快照模型既有错误。
   早于本批最小 `effective_at` 的断点不受候选行影响，免重复校验。
4. 任一断点失败：整批回滚，**不消耗序号**（下一批仍取同一序号），历史字节不变。
5. 全部断点通过后才写入 `batches` 与 `revisions` 并提交。

## 查询规则

`query --effective-at T --known-seq K`：

1. 先取不超过 `K` 的最大已接受批次 `known_seq`（无则返回 `state=absent`）。
2. 逐任务在该可见集合内取不晚于 `T` 的最大 `(effective_at, known_seq, seq_in_batch)`
   修订；同一生效时刻由不同批次写入时，入库序号大的更正生效。
3. 命中 `delete` 或无任何不晚于 `T` 的修订时，该任务不存在；剩余任务组成快照。
4. 对快照执行与快照 CLI 完全相同的关键路径计算，输出原工期、最早/最晚起止、
   总时差/自由时差、关键活动/关键边与一条确定关键路径。
5. 每个存活任务附 `source`（`batch_id`、`known_seq`、`seq_in_batch`、
   `effective_at`、`op`）作为修订来源证据；另有 `revision_sources` 汇总。

## CLI

```sh
# 导入（可选 --output 原子发布回执；数据库不存在时自动建表）
python -m project_path.history import --db run/history.db \
    --source fixtures/history/batch-01-baseline.json --output run/import-1.json
# 重放（坐标均为必填整数）
python -m project_path.history query --db run/history.db \
    --effective-at 5 --known-seq 2 --output run/query.json
```

导入成功向 stdout 打印回执（含 `known_seq` 与 `status=accepted|replayed`）；
校验或 I/O 失败退出码 2 并写 stderr，数据库与既有输出保持不变。
数据库路径拒绝符号链接；`--db`、`--source`、`--output` 不得指向同一路径；
`--output` 经 `common.save` 同目录临时文件 + `os.replace` 原子发布，失败保留旧报告。
对不存在的库执行 `query` 直接报错且不创建文件。

## 持久化结构

```text
batches(batch_id PRIMARY KEY, known_seq UNIQUE, source_sha256, source_blob)
revisions(known_seq, seq_in_batch, task_id, effective_at, op,
          duration NULLABLE, predecessors JSON, batch_id,
          PRIMARY KEY(known_seq, seq_in_batch))
```

单操作者本地工具，沿用快照链路的边界：不承诺多进程并发写与断电恢复；
无网络访问、无第三方依赖。

把已接受修订导出为可移交数据湖事件集（JSONL 分区 + 清单、续导与故障恢复）的
`lake-export` 子命令见 [lake-export.md](lake-export.md)；该导出为纯事件转储，
不改变本文件定义的重放语义。
