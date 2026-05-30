# Truck Agent 后续计划

## 协作约定

每次代码更新同步维护：

- `status.md`：当前文件结构、职责、核心数据结构和本次改动。
- `plan.md`：未完成 TODO、下一次目标和实施顺序。

讨论下一次修改方向时，先在本文件确认目标，再开始编码。

## 当前目标

MVP 已完成结构简化。轨迹记忆、三类休息 DSL 与 progress、货物品类、区域限制、距离限制和区域访问额度 DSL 已完成。`recurring_action_blackout` 暂缓；下一次优先讨论并实现 `scheduled_route` 状态机 DSL。

## TODO

### MVP 验收

- [x] 运行无写入 AST 语法检查与 `demo/agent/tests/test_mvp.py`，7 个测试通过。
- [x] 使用本地评测入口跑 1 天仿真，确认 `calc_monthly_income.py` 无 `validation_error`。
- [x] 使用本地评测入口跑 31 天仿真：140 步，约 39.62 秒，总净收益 `-106867.23`，无 `validation_error`。
- [x] 简化 MVP：删除 `domain.py` 和 `observation.py`，模拟结果收敛为完成态、`metrics` 或失败原因。
- [x] 简化后重新运行 31 天回归：仍为 140 步，总净收益保持 `-106867.23`，无 `validation_error`。

### 下一次建议目标

- [x] 新增 `trajectory_memory.py` 和对应单元测试，为偏好 progress 提供稳定输入。
- [x] 新增 DSL 类型定义与规则注册表，但暂不接 LLM 编译。
- [x] 使用手工构造 DSL 测通 `daily_continuous_rest`、`daily_rest_window`。
- [x] 精简两类休息 progress 与根目录调试输出，仅保留后续决策需要的字段。
- [x] 实现 `monthly_off_day_quota` DSL 与 progress。
- [x] 精简 `monthly_off_day_quota` progress，仅保留后续决策所需字段。
- [x] 实现并精简 `cargo_category_policy` DSL，删除提前实现的独立 evaluation。
- [x] 将全部 JSON 调试输出迁入根目录 `test_output/`，后续统一写入该目录。
- [x] 实现并精简 `region_policy` DSL，删除 `subjects`，默认同时约束车辆位置、订单起点和终点。
- [x] 实现 `distance_limit` DSL：统一表达单笔距离限制与月度累计空驶限制，并增加 `cumulative` progress。
- [x] 将 `distance_limit` 新增代码的注释、报错和调试输出说明统一调整为中文。
- [x] 实现 `region_visit_quota` DSL 与 progress，支持按车辆到达或订单装卸触点统计不同自然日或访问次数。
- [ ] 下一次实现 `scheduled_route` 状态机 DSL。
- [ ] 接入 forced action 门并重新运行 baseline，优先消除 D001 `74400.00` 和 D002 `55800.00` 的每日休息罚分。

### P0 第一批：轨迹基础设施

- [x] 新增 `trajectory_memory.py`，通过 `query_decision_history(driver_id, 1)` 增量维护历史，缺口或重置时自动全量重建。
- [x] 验证轨迹快照：覆盖增量追加、防重复、跨日 wait 拆分、缺口重建和调用方修改隔离。
- [x] 新增偏好 DSL 数据结构和 `preferences/registry.py`，当前注册三类休息规则。
- [ ] 新增偏好编译缓存，仅在首次见到文本或文本变化时调用 LLM。
- [x] 对已实现休息类型校正 `progress_kind`：固定为 `daily`，并由代码注册表决定处理器。
- [x] 为 `monthly_off_day_quota` 增加 `monthly_quota` 校正。
- [ ] 随新增类型继续扩展 `progress_kind` 校正。

### P0 第二批：规则与候选

- [x] 实现 `daily_continuous_rest`、`daily_rest_window` DSL 与 progress。
- [x] 实现 `monthly_off_day_quota` DSL 与 progress。
- [x] 实现 `cargo_category_policy`。
- [x] 实现 `region_policy` DSL。
- [x] 实现 `distance_limit` DSL 与月度累计空驶 progress。
- [ ] 后续按扩展样本需要实现 `recurring_action_blackout` DSL。
- [x] 实现 `region_visit_quota` DSL 与 progress。
- [ ] 静态与动态规则稳定后，统一设计候选 evaluation。
- [ ] 实现 `scheduled_route` 状态机 DSL，统一表达指定日期停留与有序多地点任务。
- [ ] 增加 forced action 门，在必要休息时跳过扫描。
- [ ] 增加休息类候选投影评估，过滤执行后无法补足休息的订单。
- [ ] 扩展 wait 候选：补足休息、等到窗口结束、等到午夜、短等待。
- [ ] 扩展 reposition 候选：DSL 目标点、扫描货源聚类、历史热点。

### P0 第三批：LLM 仲裁

- [ ] 仅在多个安全候选接近或偏好冲突复杂时调用 LLM。
- [ ] LLM 只返回 `candidate_id`，不得自由编写货源 ID 或经纬度。
- [ ] 模型失败、超时、非法输出时回退最高分安全候选。

### P1/P2

- [ ] 自适应 `query_cargo(k)`。
- [ ] 区域与时段价值记忆。
- [ ] 目的地价值和单步前瞻。
- [ ] 自动调参、rollout、多模型路由和结果分析工具。
