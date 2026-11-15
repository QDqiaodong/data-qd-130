# data-qd-130 · 项目关键路径分析

Precedence scheduling and total float with critical path evidence。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/project-path --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/project_path/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## SQLite双时态修订仓

快照CLI保持不变；另有 `project_path.history` 把计划变更按批次存入本地SQLite（仅标准库），
支持乱序补录与按 `effective_at`、`known_seq` 重放，语义与持久化结构见
[双时态契约](docs/bitemporal.md)。每批带 `batch_id`，修订按任务给出非负整数生效时刻，
`upsert` 完整替换任务、`delete` 表示不存在；同批同任务同生效时刻重复非法，跨批允许更正。
接受前在单事务内校验候选历史的全部生效断点（空快照/悬空依赖/成环即整批回滚且不消耗序号）；
相同 `batch_id` 与相同源字节重试返回原序号，异内容冲突；旧 `known_seq` 结果永久可复现。

```sh
.venv/bin/plan-history import --db run/history.db \
  --source fixtures/history/batch-01-baseline.json --output run/import-1.json
.venv/bin/plan-history query --db run/history.db --effective-at 4 --known-seq 2
# 或不安装直接运行：python3 -m project_path.history ...
```

查询输出每个存活任务的修订来源（批次、序号、生效时刻、操作），并附与快照CLI一致的
原工期、时差与关键路径证据；`--output` 复用同一套原子安全发布，失败退出2且不破坏旧产物。

## 业务与样例结果

样例项目工期8，关键路径A→C→D，B总时差2、独立E总时差6；显式无限资源假设。

## 资源受限排程（`plan-schedule`）

新增入口 `project_path.resource_cli`（安装后命令 `plan-schedule`）在双时态修订仓之上
按 `(effective_at, known_seq)` **只读**取得计划，再读取资源JSON（每类可再生资源的整数
容量与每任务各资源整数需求），求**全局最短工期**及该工期下按任务ID升序
**开始时刻向量字典序最小**的最优排程。输出逐任务起止、分段资源用量、静态关键路径下界
与修订来源；贪心结果绝不当最优发布，`--time-limit` 到期直接失败而不降级。
完整输入/输出契约、语义与样例见 [资源受限排程契约](docs/resource-scheduling.md)。

```sh
.venv/bin/plan-history import --db run/history.db \
  --source fixtures/history/batch-01-baseline.json
.venv/bin/plan-schedule --db run/history.db --effective-at 0 --known-seq 1 \
  --resources fixtures/resources.json --output run/schedule.json
# 或不安装直接运行：python3 -m project_path.resource_cli ...
```

样例最短工期10（静态关键路径下界8）：A(0–3)、B(3–5)、C(5–9)、D(9–10)、E(0–2)，
B与C争用inspector；D在C完成时刻9立即开始，验证同刻释放可复用。

## 数据湖事件集导出（`plan-history lake-export`）

`plan-history` 另有子命令 `lake-export`，把修订仓已接受的修订导出为可移交数据湖
事件集：按 `known_seq` 分区、`seq_in_batch` 排序的 JSONL 事件（批次、双时间、操作、
完整任务值/删除标记、源字节SHA256），外加记录上游标识、连续水位、行数与分区哈希的
`manifest.json`。纯事件导出，不计算快照、关键路径或排程。源库只读且字节不变；
缺序不跳过、已提交分区不可覆盖、同范围重跑幂等、新批次只追加；新分区暂存后原子
落盘，manifest 是唯一提交点，落盘到提交间故障的孤儿分区按哈希接纳，失败不推进
水位。完整契约见 [数据湖事件集导出](docs/lake-export.md)。

```sh
.venv/bin/plan-history lake-export --db run/history.db --lake run/lake \
  --output run/export.json
# 或不安装直接运行：python3 -m project_path.history lake-export ...
```

fixtures/lake/ 为由 fixtures/history/ 三个批次生成的样例事件集。

## 实绩资源结算（`plan-billing`）

新增入口 `project_path.billing_cli`（安装后命令 `plan-billing`）：读取实绩执行 JSON
（唯一任务 ID、`0 ≤ 起 ≤ 止` 整数、每资源非负整数需求）与计价 JSON（整数容量、账期
边界、累进用量阶梯、非负整数分单价），逐身份段核验瞬时容量，按左闭右开区间计量、
按任务用量分摊段费用，并把发票、明细与累计用量写入 SQLite。**明确契约，不做排程**。

同一资源/账期跨票共享阶梯：每票在 `BEGIN IMMEDIATE` 写锁内读取已提交累计用量后重算，
不会沿用过期用量；同 `(资源, 账期)` 的各明细先精确累加，再用最大余数法落到整数分，
平局取较小任务 ID。`invoice_id` 与两份源字节哈希绑定：同 ID 同字节重试直接复用且不
重复扣量，异内容冲突，任一失败整体回滚。完整契约、样例金额与并发语义见
[实绩资源结算](docs/resource-billing.md)。

```sh
.venv/bin/plan-billing settle --db run/billing.db --invoice-id INV-1 \
  --execution fixtures/actuals-handover.json \
  --pricing fixtures/pricing-tiers.json --output run/invoice.json
.venv/bin/plan-billing query --db run/billing.db --invoice-id INV-1
# 或不安装直接运行：python3 -m project_path.billing_cli ...
```

样例中 A、B 以相同 crew=2 用量在时刻30交接、C 占用 inspector=1；账期在50切分，crew
首档60单位7分、其后4分。发票合计 950 分：crew 账单期1为 580（A 420 / B 160），
crew 账单期2为 280（B），inspector 90（C）。交接时刻落在阶梯边界时，A、B 的费用
归属随身份改变，而总额不变。
