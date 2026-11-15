# 资源受限排程 v1（精确最优）

在双时态修订仓之上新增 `plan-schedule`：按 `(effective_at, known_seq)` 坐标**只读**取得
计划快照，再结合资源 JSON 求全局最短工期与字典序最小的最优排程。既有快照入口
`project-path` 与修订仓入口 `plan-history` 的输入输出与行为均不变。

仅使用 Python 标准库；除运行 `--time-limit` 超时保护外，不做任何外部求解器调用。

## 输入契约

### 计划来源（只读）

`--db` 指向 `plan-history import` 建立的 SQLite 修订仓，`--effective-at` 与
`--known-seq` 为必填非负整数，语义与 [bitemporal.md](bitemporal.md) 的 `query` 完全一致：

* 可见批次为不超过 `--known-seq` 的最大已接受批次；
* 每任务取不晚于 `--effective-at` 的最大 `(effective_at, known_seq, seq_in_batch)` 修订；
* `delete` 或无命中即该任务不存在；全部不存在即坐标为 `absent`，本命令失败退出 2。

数据库以 `file:...?mode=ro` 只读打开：不存在时**不会创建**，查询与排程**不会改写**
任何字节。快照任务沿用快照契约的 `id`、`duration`（0..10^6 整数）、`predecessors`
规则，`analyze` 的校验（悬空依赖、成环、重复、越界）照旧生效。受资源排程支持的规模为
至多 **16 个任务**、至多 **3 类可再生资源**；超出即明确失败（退出 2），不会静默简化。

### 资源文档

```json
{
  "resources": [
    {"name": "crew", "capacity": 2},
    {"name": "inspector", "capacity": 1}
  ],
  "demands": [
    {"task_id": "A", "demand": [2, 0]},
    {"task_id": "B", "demand": [0, 1]},
    {"task_id": "C", "demand": [1, 1]},
    {"task_id": "D", "demand": [0, 1]},
    {"task_id": "E", "demand": [0, 0]}
  ]
}
```

* 顶层字段恰为 `resources`、`demands`；重复键、非有限数值等沿用 `common.read` 的严格读取。
* `resources` 为 1..3 个资源类，`name` 唯一且为标识符，`capacity` 为非负整数（≤10^9）。
  类的顺序即 `demand` 向量的列顺序。
* `demands` 必须**恰好覆盖**快照中的每个任务一次（含 0 工期里程碑），不得出现未知任务；
  `demand` 长度等于类数，元素为非负整数。
* **需求超过容量**或**缺失任务配置**（以及重复、宽度不符、未知任务）都使整次运行失败，
  不产生部分报告。
* 0 工期里程碑不占资源；其 `demand` 仍按上述规则校验，但排程与分段用量中不计入。

## 输出契约

成功时写 JSON（`--output` 经 `common.save` 同目录临时文件 + `os.replace` 原子发布），
stdout 同时打印同一份报告。结构固定：

```json
{
  "schema_version": 1,
  "coordinate": {
    "requested_effective_at": 0,
    "requested_known_seq": 1,
    "effective_at": 0,
    "effective_known_seq": 1
  },
  "snapshot_revision_sources": {"A": {"batch_id": "base", "known_seq": 1,
                                      "seq_in_batch": 1, "effective_at": 0,
                                      "op": "upsert"}},
  "resource_source_sha256": "…",
  "result": {
    "resource_classes": ["crew", "inspector"],
    "capacity": {"crew": 2, "inspector": 1},
    "assignments": [
      {"task_id": "A", "duration": 3, "predecessors": [],
       "start": 0, "finish": 3, "demand": {"crew": 2, "inspector": 0}}
    ],
    "start_vector": {"A": 0, "B": 3, "C": 5, "D": 9, "E": 0},
    "makespan": 10,
    "resource_segments": [
      {"start": 0, "end": 3, "usage": {"crew": 2, "inspector": 0}}
    ],
    "static_critical_path_lower_bound": 8,
    "critical_path_evidence": {
      "one_critical_path": ["A", "C", "D"],
      "critical_activities": ["A", "C", "D"],
      "unbounded_resource_project_duration": 8
    },
    "optimality": {
      "proven_optimal": true,
      "goal": "minimum makespan, then lexicographically least start vector by task id",
      "makespan": 10,
      "static_lower_bound_tight": false,
      "search_nodes": 123,
      "method": "exact state-space enumeration of active schedules",
      "time_unit_enumeration": false
    }
  }
}
```

* `assignments` 与 `start_vector` 均按任务 ID 升序；`start`/`finish` 为整数，
  区间左闭右开 `[start, finish)`；`duration=0` 时 `start == finish`。
* `resource_segments` 只在任务开始/结束等事件点切分，逐段给出各类用量；相邻且用量完全
  相同的区段会合并（例如 E 与 A 重叠但 E 需求为零时不会造成多余切分）；不逐时间单位输出。
* `static_critical_path_lower_bound` 是无限资源静态关键路径工期，仅作下界证据；
  资源受限最优值可能严格更大。`static_lower_bound_tight` 说明该下界是否恰好取到。
* `coordinate.effective_known_seq` 是实际可见的最大批次序号，可小于请求的 `--known-seq`；
  `snapshot_revision_sources` 给出每个存活任务的修订来源（批次、序号、生效时刻、操作）。
* `optimality.proven_optimal` 恒为 `true`（失败不会发布报告）；算法从不把贪心结果
  冒充最优。`--time-limit` 到期时以退出码 2 失败，保留旧报告，不发布任何次优解。

## 排程语义与目标

任务不可抢占、finish-to-start；执行区间左闭右开，同一时刻释放的容量可立即复用。
资源为可再生整数容量。目标按两级字典序：

1. 全局最短工期（makespan）；
2. 在该工期下，按任务 ID 升序排列的开始时刻向量字典序最小。

求解器枚举**活动排程**（串行 SGS：每步选一个前驱已完成的任务并放在最早可行时刻；
0 工期里程碑强制取最早开始）。活动排程集合必含某个最优解；以
`(已排正工期任务集, 其开始时刻)` 记忆化状态，配合静态尾长、资源工作量、最晚完成窗口
与区间能量推理剪枝。所有下界只在事件时刻求值，复杂度与工期绝对值无关，因此
百万级工期仍只处理稀疏事件。该过程是精确枚举，不是贪心或启发式。

最坏情况仍是指数级：16 任务的高争用实例可能需要数十秒。`--time-limit`（默认 60 秒）
只作保护，**到期即失败**，不会用当时的贪心或部分搜索结果冒充最优。

失败语义：坐标为 `absent`、资源文档违约、快照校验失败、I/O 错误或超时，均退出 2 并写
stderr；`--output` 旧文件保持不变，源 SQLite 不被改写（只读连接）。输入、资源与输出
路径不得相同；输出符号链接拒绝。单操作者批处理，不承诺并发或断电恢复。

## CLI

```sh
python -m project_path.history import --db run/history.db \
    --source fixtures/history/batch-01-baseline.json
python -m project_path.resource_cli --db run/history.db \
    --effective-at 0 --known-seq 1 \
    --resources fixtures/resources.json --output run/schedule.json
```

安装后等价入口：`plan-schedule --db … --effective-at … --known-seq … --resources …`。
`--time-limit` 默认 60 秒，仅作超时保护，不影响精确性。

## 样例

`fixtures/input.json` 的任务 A(3)、B(2，父 A)、C(4，父 A)、D(1，父 B/C)、E(2) 配
`fixtures/resources.json` 的 `crew=2`、`inspector=1`：

| 任务 | 工期 | 需求(crew, inspector) | 开始 | 完成 |
| --- | --- | --- | --- | --- |
| A | 3 | (2, 0) | 0 | 3 |
| B | 2 | (0, 1) | 3 | 5 |
| C | 4 | (1, 1) | 5 | 9 |
| D | 1 | (0, 1) | 9 | 10 |
| E | 2 | (0, 0) | 0 | 2 |

最短工期 10；静态关键路径下界 8（A→C→D）；B 与 C 争用 inspector，E 不受限。
`D` 在 `C` 完成时刻 9 立即开始，验证同刻释放可复用。
