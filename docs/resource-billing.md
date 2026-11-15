# 实绩资源结算 v1（`plan-billing`）

在资源受限排程之上新增独立入口 `plan-billing`：读取**实绩执行文档**与**计价文档**，
核验瞬时资源容量，按账期与累进阶梯结算一张发票，并把发票、明细与累计用量写入本地
SQLite 账本。**明确契约，不做排程**：执行文档给出既成事实的区间，本命令只校验、计量
与计价，从不移动任务、从不求最短工期。

仅使用 Python 标准库。时间与数量均为整数，金额一律为**整数分**。

## 输入契约

### 执行文档（`--execution`）

```json
{
  "resources": ["crew", "inspector"],
  "tasks": [
    {"task_id": "A", "start": 0, "finish": 30, "demand": [2, 0]},
    {"task_id": "B", "start": 30, "finish": 70, "demand": [2, 0]},
    {"task_id": "C", "start": 20, "finish": 50, "demand": [0, 1]}
  ]
}
```

* 顶层字段恰为 `resources`、`tasks`；重复键、`NaN`/`Infinity` 沿用 `common.read`
  的严格读取，单文件上限 64 MB（十万级区间）。
* `resources` 为 1..32 个资源名，唯一且为标识符；其顺序即 `demand` 向量的列顺序。
* `tasks` 为 1..100000 项，`task_id` 唯一。`start`、`finish` 为整数，满足
  `0 ≤ start ≤ finish ≤ 10^12`；区间左闭右开 `[start, finish)`，故 `start == finish`
  的零宽任务合法且不占容量。`demand` 长度等于资源数，元素为非负整数（≤10^9）。
* 同一任务只允许一个区间；不表达抢占，需要拆分时给不同 `task_id`。
* **不校验依赖与历时**：本命令不排程，只接受实绩。

### 计价文档（`--pricing`）

```json
{
  "capacity": {"crew": 2, "inspector": 1},
  "periods": [
    {"period_id": "2026-01", "start": 0, "end": 50},
    {"period_id": "2026-02", "start": 50, "end": 100}
  ],
  "tiers": [
    {"resource": "crew", "size": 60, "unit_price": 7},
    {"resource": "crew", "size": null, "unit_price": 4},
    {"resource": "inspector", "size": null, "unit_price": 3}
  ]
}
```

* 顶层字段恰为 `capacity`、`periods`、`tiers`。
* `capacity` 必须以执行文档的资源名**逐一对应**，值为非负整数容量（≤10^9）。
* `periods` 为 1..100000 个账期，每项 `period_id` 唯一、`start < end` 且
  `0 ≤ start < end ≤ 10^12`。文档内的先后顺序无关；按 `start` 排序后**不得重叠**。
  所有**正用量**时刻都须被某个账期覆盖；出现未被覆盖的用量即整张发票失败。
  账期可以留空档，空档内不得有正用量。
* `tiers` 为每个资源各自的累进阶梯，按文档出现顺序自上而下累积。除最后一档外每档
  `size` 为正整数，最后一档 `size` 为 `null`（无上限），保证所有用量都可计价；
  每个资源至少一档。`unit_price` 为每单位**分**，非负整数（≤10^9）。
* 计价文档只描述容量、账期与阶梯，**不含排程**。

## 语义

### 身份分段与容量核验

`rcpsp.resource_segments(..., keep_identity=True)` 在任务开始/结束事件点切段，并保留
每段**活跃任务集合**；仅当活跃集合不变时才合并相邻段。于是“用量完全相同但责任任务
改变”的交接仍留作两段——这是按任务归属费用的前提。零工期任务不产生切段。

容量核验逐段比较 `usage ≤ capacity`；任一段超出即 `capacity exceeded`，**整张发票
拒绝入账**，不产生任何部分结果。同一时刻释放的容量立刻可复用：A 的 `finish` 恰为 B 的
`start` 时两者不重叠。核验与结算使用同一份身份分段，结论与计量不可能互相矛盾。

### 账期与累进阶梯

1. 身份分段按账期边界再切分（只访问事件点与账期边界，从不逐时刻枚举）。
2. 累进阶梯按 `(资源, 账期)` 共享累计用量：同一资源在每个账期**重新**从零累计阶梯，
   账期之间互不影响。一个账期内，各段按**时间顺序**自累计起点继续计价。
3. 每段费用再用 `Fraction` 精确分摊给该段活跃任务，分摊权重为任务在该资源上的
   `demand` 占段用量的比例（与段时长、账期无关；跨账期的任务由两段按实际宽度分别
   计入）。跨阶梯的段按精确阶梯计价后再按同一权重分摊，保证费用随任务身份移动。
4. 单张发票内，同一 `(资源, 账期)` 的各项精确费用**先精确累加**，再用**最大余数法**
   落到整数分：向下取整后，把差额按小数余数从大到小逐分补齐，余数相同则任务 ID
   较小者优先。因此各明细之和恒等于该组精确整数费用（组总额即阶梯计价总额），
   四舍五入不创造也不丢失金额。
5. 同 `(资源, 账期)` 的累计用量以 SQLite 账本中**已提交**的发票为准。新发票从该
   累计值继续阶梯，不重复占用已用额度。

**身份交接示例**（`fixtures/actuals-handover.json` + `fixtures/pricing-tiers.json`）：
A 在 `[0,30)`、B 在 `[30,70)` 都用 `crew=2`，用量恒定。默认分段会合并成一段；身份
分段保留 `[0,20)`、`[20,30)`、`[30,50)`、`[50,70)` 四段（C 的 inspector 用量另计）。
账期在 50 切分、crew 阶梯为 60 单位内 7 分、超出 4 分：

| 资源/账期 | 用量 | 费用(分) | 明细(任务: 分) |
| --- | --- | --- | --- |
| crew / 2026-01 | 100 | 580 | A:420, B:160 |
| crew / 2026-02 | 40 | 280 | B:280 |
| inspector / 2026-01 | 30 | 90 | C:90 |
| **合计** | | **950** | |

A 用满 `[0,20)` 与 `[20,30)` 的 crew 60 单位中前 60 单位落在首档（7 分），B 的
`[30,50)` 40 单位中前 0 单位落首档、其余 40 单位落 4 分档；账期 2 的 crew 40 单位
从零重新起阶梯，全部 4 分。若把交接时刻移到阶梯边界，A、B 的费用归属会随之改变，
而发票总额不变（测试 `HandoverAtTierBoundary` 覆盖 5/10/15 三个交接点）。

## SQLite 账本

默认在 `--db` 指定的路径建立（父目录不存在则创建；符号链接拒绝）：

* `invoices`：`invoice_id` 主键、两个源字节 SHA256、原始源字节 BLOB、总金额、
  唯一且递增的 `accepted_seq`；
* `invoice_groups`：发票内每个 `(资源, 账期)` 的用量、金额及累计用量前后值；
* `invoice_lines`：每任务的明细（资源、账期、用量、整数分）；
* `usage_ledger`：`(资源, 账期) → 已提交累计用量` 的唯一事实来源。

一张票的发票、组、明细与 `usage_ledger` 更新在**同一事务**内完成，`BEGIN IMMEDIATE`
先取写锁再读取累计用量，因此不会沿用过期用量重算。所有路径失败（校验、容量、I/O、
约束）均整体回滚，不留半张票、不留已扣用量，也不消耗 `accepted_seq`。建表语句在
事务之外以 `CREATE TABLE IF NOT EXISTS` 幂等执行，所以首次失败可能留下**空表**，
但绝无发票行、明细行或用量行。

### 绑定、幂等与冲突

* 同 `invoice_id` + 两源**字节完全相同**：直接返回原结果（`status: replayed`），
  **不重复扣减用量**、不新增行，输出与首次一致。
* 同 `invoice_id` + 任一份源字节不同：`invoice_id conflict`，退出 2，库内字节不变。
* 未提供 `--invoice-id` 的查询不写库；`query` 以只读连接打开，库不存在时报错而非创建。

### 并发

同一 `(资源, 账期)` 共享阶梯，因此两张并发票的语义必须等价于**某一提交串行序**。
实现选择**持锁计算**：`BEGIN IMMEDIATE` 使第二张票等待第一张提交后再读取累计用量。
于是并发结果与某个串行顺序一致：抢先获锁者占用较便宜的阶梯，落后者承接剩余阶梯；
任何票都不会基于陈旧累计重复计价。同 `invoice_id` 的并发重试由主键与事务保证只有
一次 `accepted`，其余为重放（测试含 4 线程重试与 2 线程跨阶梯并发）。

## 输出契约

`settle` 成功输出（`--output` 经 `common.save` 原子发布，stdout 同时打印）：

```json
{
  "schema_version": 1,
  "status": "accepted",
  "invoice_id": "INV-1",
  "execution_source_sha256": "…",
  "pricing_source_sha256": "…",
  "amount_cents": 950,
  "accepted_seq": 1,
  "groups": [
    {"resource": "crew", "period_id": "2026-01", "quantity": 100,
     "amount_cents": 580,
     "cumulative_quantity_before": 0, "cumulative_quantity_after": 100}
  ],
  "lines": [
    {"task_id": "A", "resource": "crew", "period_id": "2026-01",
     "quantity": 60, "amount_cents": 420}
  ],
  "segments": 4,
  "peak_usage": {"crew": 2, "inspector": 1},
  "time_unit_enumeration": false
}
```

* `status` 为 `accepted`（首次入账）或 `replayed`（同字节重试）。
* `groups` 按资源文档顺序、账期时间顺序排列；`lines` 按组内任务 ID 升序。
* 重放结果的 `groups`、`lines` 均取自库内已提交行，与首次逐字段一致（其中
  `cumulative_*` 为首次入账时记录的值）。
* 金额均为整数分；`amount_cents` 等于所有组之和，也等于所有明细之和。
* `query` 输出 `invoice`、`ledger`、`invoices_total`、`amount_cents_total`，只读。

失败语义：任一文档违约、容量超限、账期不覆盖、`invoice_id` 冲突、I/O 或数据库错误，
均退出 2 并写 stderr；旧账本与旧 `--output` 保持不变。

## CLI

```sh
python -m project_path.billing_cli settle --db run/billing.db \
    --invoice-id INV-1 \
    --execution fixtures/actuals-handover.json \
    --pricing fixtures/pricing-tiers.json \
    --output run/invoice.json
python -m project_path.billing_cli query --db run/billing.db --invoice-id INV-1
```

安装后等价入口：`plan-billing settle …` / `plan-billing query …`。
账本、两份源与输出路径不得相同。

## 规模与边界

* 十万级任务区间、万亿级时刻：只访问事件点与账期边界，复杂度与时间跨度无关；
  10 万区间实测约 2 秒。
* 金额与用量须落在 SQLite 有符号 64 位以内，否则明确失败而非静默截断。
* 中间项（段 × 资源 × 活跃任务）超过 400 万即明确失败，避免病态全重叠输入拖死进程。
* 不支持：排程/重排程、任务抢占、按日历的费率、多币种、税额与折扣、账期外用量、
  跨账期结转的阶梯（阶梯按账期独立）、或并发之外的断电恢复保证。SQLite 事务提供
  单机原子提交；极端断电恢复依赖 SQLite 自身的日志保证，超出本契约范围。
