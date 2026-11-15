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
