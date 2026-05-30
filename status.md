# Truck Agent 项目状态

## 当前阶段

已完成 MVP 骨架：使用确定性策略完成“获取状态 -> 查询货源 -> 扫描后重新校时 -> 模拟订单 -> 过滤非法订单 -> 评分 -> 输出接单或等待动作”的最小闭环。偏好处理已开始实现：当前新增三类休息 DSL、月度累计空驶和区域访问额度 progress，以及货物品类、区域限制和距离限制 DSL，但尚未接入决策入口，也不调用 LLM。

## 当前文件结构

```text
demo/agent/
├─ model_decision_service.py       # 评测固定入口，编排 MVP 决策流程
├─ settings.py                     # Agent 自维护参数
├─ cargo_simulator.py              # 纯函数订单模拟与可执行性过滤
├─ candidate_generator.py          # 使用普通 dict 生成 take_order 和短 wait 候选
├─ scorer.py                       # 对普通 dict 候选按单位时间净收益排序
├─ safety_guard.py                 # 校验普通 dict 动作并提供 wait 兜底
├─ trajectory_memory.py            # 增量同步动作历史，输出偏好 progress 所需轨迹快照
├─ preferences/
│  ├─ __init__.py                  # 暴露偏好 DSL 构造与 progress 入口
│  ├─ registry.py                  # 根据 rule.type 分发 progress 处理器
│  ├─ rest_rules.py                # daily_continuous_rest、daily_rest_window DSL 与 progress
│  ├─ off_day_rules.py             # monthly_off_day_quota DSL 与 progress
│  ├─ cargo_rules.py               # cargo_category_policy DSL
│  ├─ region_rules.py              # region_policy DSL
│  ├─ distance_rules.py            # distance_limit DSL 与月度累计空驶 progress
│  └─ region_visit_rules.py        # region_visit_quota DSL 与月度访问额度 progress
└─ tests/
   ├─ test_mvp.py                  # MVP 单元测试
   ├─ test_trajectory_memory.py    # 轨迹记忆单元测试
   ├─ test_rest_progress.py        # 每日休息 DSL、progress 和调试输出测试
   ├─ test_monthly_off_day_progress.py # 月度整天休息额度测试
   ├─ test_cargo_category_policy.py # 货物品类 DSL 测试
   ├─ test_region_policy.py        # 区域限制 DSL 测试
   ├─ test_distance_limit.py       # 距离限制 DSL 与累计 progress 测试
   └─ test_region_visit_quota.py   # 区域访问额度 DSL 与 progress 测试
```

根目录新增统一测试输出目录：

```text
test_output/
├─ rest_progress_test_output.json     # 休息 DSL 与 progress 的可查看调试案例
├─ monthly_off_day_progress_test_output.json # 月度整天休息额度调试案例
├─ cargo_category_policy_test_output.json # 货物品类 DSL 调试案例
├─ region_policy_test_output.json    # 区域限制 DSL 调试案例
├─ distance_limit_test_output.json   # 距离限制 DSL 与累计 progress 调试案例
└─ region_visit_quota_test_output.json # 区域访问额度 DSL 与 progress 调试案例
```

## 核心数据结构

- 模块间直接传递普通 `dict`，不再维护 MVP 阶段不必要的 dataclass。
- 成功模拟结果仅保留 `action_end_minutes`、`action_end_time`、`position_after` 和 `metrics`。
- 失败模拟结果仅保留 `{"rejected": true, "reason": "..."}`。
- 候选动作使用普通 `dict`，保存候选 ID、动作参数、评分、原因和精简模拟结果。
- `AgentSettings`：集中维护查询规模、成本估计、速度估计、月底边界和等待时长。
- `TrajectoryMemory.snapshot()`：输出 `events`、`action_counts`、`active_minutes_by_day`、`wait_intervals_by_day` 和最后完成时间。后续 DSL progress 只读取该快照，不直接解析评测日志。
- 休息 DSL：使用普通 `dict` 保存静态语义。当前支持 `daily_continuous_rest`、`daily_rest_window` 和 `monthly_off_day_quota`。
- `calculate_rule_progress(rule, trajectory_snapshot, now_minutes)`：根据 `rule.type` 调用对应处理器，返回独立的动态 progress，不修改 DSL。
- 连续休息 progress：输出当日最长连续等待、当前尾部连续等待、剩余余量和 `forced_now`。
- 固定窗口 progress：输出当前或下一个窗口实例、距离窗口开始分钟数、已休息分钟数、缺失分钟数、建议等待分钟数和 `forced_now`。
- 月度整天休息 progress：输出已完成 off days、剩余欠账、剩余可用自然日、松弛天数、当前日是否仍可保留和 `forced_now`。
- `cargo_category_policy`：仅保存禁止接取的货物品类。后续统一候选评估器再结合订单 `cargo_name` 判断是否违反偏好。
- `region_policy`：使用 `stay_inside` / `stay_outside` 表示区域内活动或区域禁入；支持 `named`、`bbox`、`circle` 区域；默认同时约束车辆当前位置、订单装货点和卸货点。
- `distance_limit`：单笔规则支持 `pickup_deadhead_km`、`haul_distance_km`；月度累计规则使用 `monthly_deadhead_km` 和 `cumulative` progress。累计空驶只统计接货空驶与主动 `reposition`。
- `TrajectoryMemory.events[].metrics`：仅保留历史动作中的 `pickup_deadhead_km`、`haul_distance_km`、`distance_km`，为累计距离 progress 提供事实输入。
- `region_visit_quota`：支持 `position_arrival` 和 `cargo_touch` 两种事件，以及 `distinct_day` 和 `occurrence` 两种计数方式。progress 输出已完成次数、剩余次数、剩余自然日、松弛天数和满足状态。
- `TrajectoryMemory.events[].cargo_touch_points`：可选的订单装卸触点数组，为 `cargo_touch` 区域额度提供完整事实；缺省时可使用卸货后的 `position_after` 回退统计。

### TrajectoryMemory 快照示例

```json
{
  "driver_id": "D001",
  "total_steps": 2,
  "last_step_end_minutes": 530,
  "action_counts": {"take_order": 1, "reposition": 0, "wait": 1},
  "active_minutes_by_day": {"0": 60},
  "wait_intervals_by_day": {
    "0": [
      {
        "start_minutes": 65,
        "end_minutes": 530,
        "duration_minutes": 465,
        "position": {"lat": 23.1, "lng": 113.1}
      }
    ]
  },
  "events": [
    {
      "step": 1,
      "action": "take_order",
      "params": {"cargo_id": "C1"},
      "step_start_minutes": 0,
      "action_start_minutes": 5,
      "action_end_minutes": 65,
      "step_end_minutes": 65,
      "query_scan_cost_minutes": 5,
      "action_exec_cost_minutes": 60,
      "position_before": {"lat": 23.0, "lng": 113.0},
      "position_after": {"lat": 23.1, "lng": 113.1},
      "accepted": true,
      "metrics": {
        "pickup_deadhead_km": 12.5,
        "haul_distance_km": 80.0
      },
      "cargo_touch_points": [
        {"lat": 23.05, "lng": 113.05, "at_minutes": 20, "region_names": ["增城区"]},
        {"lat": 23.1, "lng": 113.1, "at_minutes": 65}
      ]
    },
    {
      "step": 2,
      "action": "wait",
      "params": {"duration_minutes": 465},
      "step_start_minutes": 65,
      "action_start_minutes": 65,
      "action_end_minutes": 530,
      "step_end_minutes": 530,
      "query_scan_cost_minutes": 0,
      "action_exec_cost_minutes": 465,
      "position_before": {"lat": 23.1, "lng": 113.1},
      "position_after": {"lat": 23.1, "lng": 113.1},
      "accepted": null,
      "metrics": {},
      "cargo_touch_points": []
    }
  ]
}
```

`events` 是通用事实来源；按天聚合字段是休息类规则的快捷输入。当前仅新增模块，不接入决策入口。

## 本次修改

- 删除 `domain.py`，将 `Observation`、`CargoSimulation`、`ActionCandidate` 替换为轻量 `dict`。
- 删除 `observation.py`，将“扫描前状态 -> 查询货源 -> 扫描后重新校时”直接放回 `model_decision_service.py`。
- 精简 `cargo_simulator.py` 输出，只保留完成态与 `metrics`，失败时返回简短原因。
- 同步调整候选生成、排序、SafetyGuard 和 MVP 测试。
- 新增 `trajectory_memory.py`：常规刷新仅查询最后一步历史，遇到历史缺口或仿真重置时自动全量重建。
- 新增轨迹记忆测试：覆盖增量追加、防重复、跨日 wait 拆分、缺口重建和快照隔离。
- 新增 `preferences/rest_rules.py`：实现 `daily_continuous_rest` 与 `daily_rest_window` 原子 DSL 构造、校验和 progress 计算。
- 新增 `preferences/registry.py`：按照 `rule.type` 分发两类 progress 处理器。
- 新增休息 progress 测试并自动刷新 `test_output/rest_progress_test_output.json`，便于直接检查 DSL 和 progress 输出。
- 精简休息 progress：删除连续休息的 `required_wait_now_minutes`，删除固定窗口的 `window_state`、`status`，新增 `minutes_until_window_start`。
- 精简调试输出：不再写入内部计算输入 `trajectory_snapshot`；固定窗口已错过部分休息时间后不再强制等待。
- 新增 `preferences/off_day_rules.py`：实现 `monthly_off_day_quota` 原子 DSL、校验和 progress 计算，按 DSL 的 `blocked_actions` 判断自然日是否可计为 off day。
- 新增月度整天休息额度测试和 `test_output/monthly_off_day_progress_test_output.json`，覆盖早月宽松、月末强制保留、额度已不可补救、动作集合差异和月末边界。
- 精简月度整天休息 progress：删除可推导的 `completed_off_day_indexes`、`remaining_calendar_days`、`current_day_index` 和 `quota_feasible`。
- 新增 `preferences/cargo_rules.py`：实现 `cargo_category_policy` 原子 DSL 和校验，不读取 trajectory progress。
- 精简 `cargo_category_policy`：删除 `policy` 与提前实现的独立 evaluation，候选判断留给后续统一评估器。
- 新增 `test_output/` 并将三份 JSON 调试输出统一迁入；后续测试输出均写入该目录。
- 新增 `preferences/region_rules.py`：实现 `region_policy` 原子 DSL 与校验，支持命名区域、经纬度矩形、圆形区域和可选业务生效时间段。
- 新增区域限制 DSL 测试与 `test_output/region_policy_test_output.json`，覆盖惠州货源禁接、深圳不出市、圆形禁区和指定日期深圳禁入。
- 精简 `region_policy`：删除 `subjects`，区域限制默认覆盖车辆当前位置、订单装货点和卸货点。
- 新增 `preferences/distance_rules.py`：实现统一的 `distance_limit` DSL，并通过 `metric` 与 `progress_kind` 区分单笔距离限制和月度累计空驶限制。
- 扩展 `trajectory_memory.py`：历史事件保存最小距离 `metrics`，月度累计空驶 progress 可直接从快照计算。
- 新增距离限制测试与 `test_output/distance_limit_test_output.json`，覆盖单笔接货空驶、单笔承运里程、月度累计空驶、剩余额度与超出里程。
- 将本次新增距离规则的注释、校验报错和调试输出说明统一调整为中文。
- 新增 `preferences/region_visit_rules.py`：实现 `region_visit_quota` DSL 与月度访问额度 progress，支持车辆到达、订单装卸触点、不同自然日去重和按次累计。
- 扩展 `trajectory_memory.py`：保留可选的 `cargo_touch_points`，为后续完整统计订单装卸区域提供事实输入。
- 新增区域访问额度测试与 `test_output/region_visit_quota_test_output.json`，覆盖增城货源不同自然日、圆形区域到达、同日多次访问和卸货位置回退。

## 已实现边界

- Agent 只通过 `SimulationApiPort` 获取状态和货源，不读取原始数据文件。
- `query_cargo` 后重新获取时间，并以扫描后的时间过滤已失效货源。
- 对齐 Haversine 距离、空驶分钟向上取整、装货窗口等待和月底 horizon。
- 过滤车型不匹配、亏损、不可及时到达装货点和月底无法完成的订单。
- 无可接订单时等待 30 分钟；临近月底时不再扫描。

## 本次验证结果

- `python -m unittest discover -s agent\tests -v`：7 个 MVP 单元测试全部通过。
- 使用 `PYTHONDONTWRITEBYTECODE=1` 清理缓存后复跑：7 个测试再次通过，`demo/agent/` 无残留 `__pycache__`。
- 无写入 AST 解析检查：简化后 9 个 Python 文件语法通过。
- 原始数据直读扫描：`demo/agent/` 中未发现 `cargo_dataset`、`drivers.json`、`server/data` 或 `open()`。
- 1 天短仿真：D001、D002 均完成动作，无运行异常，模型 token 消耗为 0。
- 收益核对：两位司机均为 `calculation_aborted=false`、`validation_error=null`。
- 31 天正式口径 baseline：共 140 步，运行约 39.62 秒，模型 token 为 0，无 driver failure。
- 31 天 baseline 收益：总毛收入 `111361.70`，总偏好罚分 `185160.00`，总净收益 `-106867.23`。
- 简化后 31 天回归：仍为 140 步，约 42.0 秒，动作结果与收益不变，两位司机均无 `validation_error`。
- 新增轨迹记忆后：全部 12 个单元测试通过；无写入 AST 检查通过，共 11 个 Python 文件；原始数据直读扫描仍为空。
- 精简两类休息 progress 后：全部 22 个单元测试通过；无写入 AST 检查通过，共 15 个 Python 文件；原始数据直读扫描仍为空；`demo/agent/` 无残留 `__pycache__`。
- 新增 `monthly_off_day_quota` 后：全部 29 个单元测试通过；无写入 AST 检查通过，共 17 个 Python 文件；原始数据直读扫描仍为空；`demo/agent/` 无残留 `__pycache__`。
- 精简 `cargo_category_policy` 并迁移 `test_output/` 后：全部 31 个单元测试通过；无写入 AST 检查通过，共 19 个 Python 文件；原始数据直读扫描仍为空；`demo/agent/` 无残留 `__pycache__`。
- 精简 `region_policy` 后：全部 38 个单元测试通过；无写入 AST 检查通过，共 21 个 Python 文件；原始数据直读扫描仍为空；`demo/agent/` 无残留 `__pycache__`。
- 新增 `distance_limit` 后：全部 44 个单元测试通过；无写入 AST 检查通过，共 23 个 Python 文件；原始数据直读扫描仍为空；`demo/agent/` 无残留 `__pycache__`。
- 新增 `region_visit_quota` 后：全部 50 个单元测试通过；无写入 AST 检查通过，共 25 个 Python 文件；原始数据直读扫描仍为空；`demo/agent/` 无残留 `__pycache__`。

## Baseline 观察

- D001 最大损失来自每日连续休息未满足：罚分 `74400.00`。
- D002 最大损失来自每日 00:00-06:00 休息未满足：罚分 `55800.00`。
- 两位司机都没有安排整天休息；D001 还产生惠州货源和深圳日期窗口罚分，D002 还缺少增城、盘库、寿宴任务处理。
- 下一阶段应先实现轨迹记忆、休息类规则和 forced action 门，再扩展订单过滤及地点任务。

## 已知限制

- MVP 将 horizon 固定为正式自然月 `31 * 24 * 60` 分钟。使用 `--simulation-days 1` 做短仿真时，Agent 仍可能选择跨越 1 天调试边界、但可在正式月度边界内完成的订单；评测端会正确将其标记为不计收入。
- 三类休息 progress 尚未接入 `ModelDecisionService`，因此当前仿真动作和 baseline 收益不会发生变化。
- 当前评测历史原生包含卸货后的车辆位置，但不包含完整装货触点；`cargo_touch` progress 会使用卸货位置回退统计。后续接入决策入口时，需要在订单执行前后补充记录 `cargo_touch_points`，才能完整覆盖“装货点或卸货点”语义。
- 并行运行 `compileall` 和单测时，Windows 下发生 `.pyc` 替换冲突；已清理缓存，并改用无写入 AST 检查完成语法验证。
