# 数据湖事件集导出 v1（`plan-history lake-export`）

在双时态修订仓之上新增 `plan-history lake-export`：把修订仓**已接受的修订**导出为
可移交的数据湖事件集（目录 + JSONL 分区 + 清单）。它是**纯事件导出**：不物化快照、
不计算关键路径、不做资源排程；既有 `project-path`、`plan-history import/query` 与
`plan-schedule` 的行为均不变。仅使用 Python 标准库，无网络访问。

## 目录布局

```text
<lake>/manifest.json                        # 唯一提交点
<lake>/partitions/known_seq=0000000001.jsonl
<lake>/partitions/known_seq=0000000002.jsonl
...
```

* 每个已接受批次一个分区文件，按 `known_seq` 分区（文件名零填充 10 位，字典序即
  数值序）；分区内行按 `seq_in_batch` 升序，与批内行序一致。
* 序列化是确定性的（排序键、紧凑分隔符、UTF-8、`\n` 结尾）：同范围重跑产生
  **字节相同**的分区，这是幂等与哈希校验的基础。

## JSONL 事件行

每行一个 JSON 对象，字段固定为 8 个：

| 字段 | 含义 |
| --- | --- |
| `batch_id` | 批次标识（与修订仓 `batches` 表一致） |
| `known_seq` / `seq_in_batch` | 入库时间轴：批次序号与批内行序号 |
| `effective_at` | 业务时间轴：修订生效时刻 |
| `op` | `upsert` 或 `delete` |
| `task_id` | 任务标识 |
| `task` | `upsert` 时为完整任务值 `{"duration":…, "predecessors":[…]}`；`delete` 时为 `null`（删除标记/墓碑） |
| `source_sha256` | 该批次源文档字节的 SHA256（随批次保留的上游证据） |

示例（一批删除、一批 upsert）：

```jsonl
{"batch_id":"batch-02-backfill","effective_at":4,"known_seq":2,"op":"delete","seq_in_batch":1,"source_sha256":"a460…","task":null,"task_id":"E"}
{"batch_id":"batch-02-backfill","effective_at":4,"known_seq":2,"op":"upsert","seq_in_batch":2,"source_sha256":"a460…","task":{"duration":5,"predecessors":["A"]},"task_id":"B"}
```

导出**不解决**跨批更正：同一 `(task_id, effective_at)` 被不同批次更正时，各事件原样
出现在各自分区中；如何解释事件流是下游的事（重放语义见
[bitemporal.md](bitemporal.md)）。

## manifest.json

清单记录上游标识、连续水位、行数与分区哈希：

```json
{
  "schema_version": 1,
  "kind": "plan-history-lake-export",
  "upstream": {
    "batches": 3,
    "tip": {"known_seq": 3, "batch_id": "batch-03-restore",
            "source_sha256": "…"},
    "history_sha256": "…"
  },
  "watermark": 3,
  "rows": 9,
  "partitions": [
    {"known_seq": 1, "batch_id": "batch-01-baseline", "source_sha256": "…",
     "file": "partitions/known_seq=0000000001.jsonl", "rows": 5, "sha256": "…"}
  ]
}
```

* `watermark` 是**连续水位**：分区严格为 `known_seq = 1..watermark`，无缺序。
* `rows` 为全事件集总行数；每个分区条目另记 `rows` 与文件字节 `sha256`。
* `upstream.history_sha256` 是上游标识：对 `known_seq:batch_id:source_sha256` 链
  逐行哈希，标识本次导出反映的完整已接受历史；`tip` 为最高批次。清单不记录
  SQLite 文件字节哈希（与平台无关），该值只出现在运行报告的 `db_sha256` 中。

## 续导规则

* **源库只读**：以 `file:…?mode=ro` 打开，不存在时不创建、任何情况下不改写；
  导出前后各算一次源库字节 SHA256，不一致即失败（单操作者前提下的并发写守卫）。
* **缺序不得跳过**：`batches.known_seq` 必须连续为 `1..N`，出现空洞或无所属批次的
  修订行即失败，绝不跳过缺失序号继续导出。
* **已提交分区不可覆盖**：清单水位内的分区只校验、不重写；缺失或损坏即失败，
  由操作者修复（例如从源库重建整个事件集），导出器绝不"自动修复"。
* **同范围重跑幂等**：无新批次时报告 `status=up_to_date`，清单与分区字节、
  文件均不变。
* **新批次只追加**：只为 `known_seq > watermark` 的批次生成新分区；续导前校验
  已提交条目与源库逐批一致（`batch_id` + `source_sha256`），上游被整体替换即失败。
* 空修订仓（无已接受批次）直接失败，不产出空事件集。

## 提交协议与故障恢复

1. 新分区先写入同目录暂存文件（`.stage-*`），再 `os.replace` **原子落盘**到最终
   分区路径；落盘即按内容寻址（暂存时同步计算 SHA256 与行数）。
2. **发布前校验**：候选清单先做严格结构校验（水位连续、行数合计、上游标识哈希、
   规范文件名），再逐个从磁盘重哈希**全部**分区（新旧一起）核对 `sha256` 与行数。
3. **manifest 是唯一提交点**：全部校验通过后，经 `common.save`（同目录临时文件
   + `os.replace`）原子替换清单；此后新水位才对外可见。
4. **分区落盘后、提交前故障**：已落盘但未入清单的分区成为孤儿。重试时重新暂存
   同批内容并比对哈希——一致则**原地接纳**孤儿（不重写、不重复记录）；不一致即
   未知文件，**拒绝覆盖**并失败。与本次导出无关的陌生文件一律不记录、不动。
5. **失败不得推进水位**：任何一步失败都在清单提交之前，旧清单（及旧水位）保持
   不变；暂存文件随失败清理，孤儿分区留待下次按哈希接纳。

## CLI

```sh
python -m project_path.history lake-export --db run/history.db \
    --lake run/lake --output run/export.json
# 安装后等价入口：plan-history lake-export --db … --lake …
```

成功向 stdout 打印报告（`--output` 经同一套原子发布写同一份 JSON）：

```json
{
  "schema_version": 1,
  "status": "exported | up_to_date",
  "previous_watermark": 0,
  "watermark": 3,
  "batches_total": 3,
  "partitions_written": [1, 2, 3],
  "partitions_adopted": [],
  "rows_exported": 9,
  "rows_total": 9,
  "db_sha256": "…",
  "history_sha256": "…"
}
```

`partitions_written` 为本次原子落盘的新分区，`partitions_adopted` 为按哈希接纳的
孤儿分区。校验或 I/O 失败退出码 2 并写 stderr，旧清单、旧分区与旧 `--output`
保持不变。`--db`、`--lake`、`--output` 不得指向同一路径；`--output` 不得位于湖目录
内（防止覆盖清单或分区）；湖目录、清单、分区路径均拒绝符号链接。

## 边界

单操作者批处理，沿用本仓既有约定：不承诺多进程并发写与断电恢复；清单经
`common.read` 严格读取（≤8 MB，约 5 万分区量级）；分区内容流式读写，内存占用与
事件集规模无关（百万行导出已验证）；无网络访问、无第三方依赖。

## 样例

[fixtures/lake/](../fixtures/lake/) 是由 [fixtures/history/](../fixtures/history/)
三个批次（基线、乱序补录含删除、删除后恢复）经本命令生成的完整事件集样例，
测试逐字节比对保证其确定性。
