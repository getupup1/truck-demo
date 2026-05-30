# 偏好 DSL 运行时数据流草案

## 1. 目标与边界

本文描述 `preference_dsl.md` 之后的运行时流程。当前项目已经有：

- `CargoSimulator`：模拟接单后的完成时间、位置和收益指标。
- `TrajectoryMemory`：从历史动作生成轨迹快照。
- `CandidateGenerator`：生成接单和短等待候选。

当前已实现三类休息规则、月度累计空驶和区域访问额度的 progress 计算器：`daily_continuous_rest`、`daily_rest_window`、`monthly_off_day_quota`、`distance_limit`、`region_visit_quota`。forced action 门、候选偏好评估器和扩展候选仍待实现。

偏好规则是软约束：最终目标仍然是最大化月度净收益。但对于“现在不休息就必然违约”的候选，可以在策略层直接禁用，避免为了短期收益稳定地产生高额罚金。

## 2. 完整数据流

```text
get_driver_status(driver_id)
        ↓
可见偏好文本 + penalty
        ↓ 首次出现或文本变化时编译并缓存
CompiledPreference DSL
        +
TrajectoryMemory.refresh(driver_id)
        ↓
TrajectoryMemory.snapshot()
        ↓ 根据 rule.type 分发给规则处理器
rule_progress
        ↓
预查询 forced action 门
        ├─ 必须立刻 wait / reposition → 直接输出动作，不扫描货源
        └─ 当前仍有余量
                ↓
          query_cargo()
                ↓ 扫描会推进仿真时间
          再次 get_driver_status()
                ↓
          模拟订单 + 生成 wait / reposition 候选
                ↓
          对每个候选做 projected evaluation
                ↓
          过滤不可执行或会造成不可修复违约的候选
                ↓
          收益 - 成本 - 偏好罚分风险 + 偏好奖励
                ↓
          程序保留 top candidates
                ↓
          可选 LLM 仲裁：只能从 candidate_id 中选择
                ↓
          SafetyGuard 再校验并输出动作
```

forced action 门必须放在 `query_cargo()` 之前，因为扫描货源本身会消耗分钟数。已经到达最晚休息时刻时，继续扫描也可能让原本可满足的规则变成不可满足。

## 3. DSL 字段如何参与判断

评估是否违反偏好时，不是只看某一个字段。核心入口是 `rule.type`，具体阈值来自 `rule.params`。

| 字段 | 作用 |
| --- | --- |
| `rule.type` | 选择规则处理器。例如 `daily_rest_window` 使用固定休息窗口处理器。 |
| `rule.params` | 保存实际判断条件。例如窗口、最低分钟数、区域、次数或距离上限。 |
| `rule.progress_kind` | 指定 progress 的维护方式。它不直接判断违约，也不应使用 `true / false`。 |
| `rule.active_period` | 判断规则当前是否生效。没有明确日期限制时为 `null`。 |
| `penalty.mode / amount / cap` | 将违约次数或超额单位转换为预计罚金。 |
| `combine` | 组合多个原子规则。例如“23 点前回家，并且夜间不出车”。 |
| `rule_id` | 关联 DSL、progress 和评估结果，便于日志与调试。 |
| `source_text` | 保留解释依据，不参与程序判断。 |

推荐使用注册表按 `type` 分发，而不是让 LLM 在每一步重新理解规则：

```text
daily_continuous_rest  → DailyContinuousRestHandler
daily_rest_window      → DailyRestWindowHandler
monthly_off_day_quota  → MonthlyOffDayQuotaHandler
...
```

`progress_kind` 只是一种维护提示和校验信息。例如：

- `daily_continuous_rest` 的 `progress_kind` 为 `daily`，每天重置。
- `region_visit_quota` 的 `progress_kind` 为 `monthly_quota`，按月累计并计算剩余次数。
- `cargo_category_policy` 的 `progress_kind` 为 `none`，只检查当前订单。

`progress_kind: none` 的规则不调用 `TrajectoryMemory.snapshot()`。例如 `cargo_category_policy` 只保存禁止接取的货物品类，后续由统一候选评估器读取订单 `cargo_name` 并判断是否违反偏好。

`region_policy` 同样不生成 progress。DSL 通过 `policy` 表示必须留在区域内或必须避开区域，默认同时约束车辆当前位置、订单装货点和卸货点。区域可以使用城市名称、经纬度矩形或圆形范围。

## 4. 三层运行时数据

### 4.1 静态 DSL

LLM 只在偏好首次出现或文本变化时编译一次：

```json
{
  "preference_id": "P002",
  "source_text": "每天凌晨0点到6点必须停车休息。",
  "penalty": {"mode": "per_day", "amount": 1800, "cap": 55800},
  "rules": [
    {
      "rule_id": "P002.R1",
      "type": "daily_rest_window",
      "progress_kind": "daily",
      "active_period": null,
      "params": {"window": {"start": "00:00", "end": "06:00"}}
    }
  ]
}
```

### 4.2 实时 progress

规则处理器读取 DSL 和 `TrajectoryMemory.snapshot()`，每轮重新计算动态状态：

```json
{
  "rule_id": "P002.R1",
  "type": "daily_rest_window",
  "as_of_time": "2026-03-08 23:00:00",
  "window_instance": {
    "start_minutes": 11520,
    "start_time": "2026-03-09 00:00:00",
    "end_minutes": 11880,
    "end_time": "2026-03-09 06:00:00"
  },
  "minutes_until_window_start": 60,
  "required_minutes": 360,
  "rested_minutes": 0,
  "missing_elapsed_minutes": 0,
  "remaining_window_minutes": 360,
  "latest_safe_busy_end_minutes": 11520,
  "latest_safe_busy_end_time": "2026-03-09 00:00:00",
  "recommended_wait_minutes": 0,
  "forced_now": false
}
```

progress 不是 DSL 的一部分，不写回 DSL 缓存。

### 4.3 候选投影评估

每个候选先模拟执行，再由所有当前生效的规则处理器评估：

```json
{
  "candidate_id": "order:C102",
  "action": "take_order",
  "simulation": {
    "action_end_time": "2026-03-09 01:20:00",
    "position_after": {"lat": 23.10, "lng": 113.60},
    "metrics": {"net_income": 3200, "net_income_per_hour": 1396}
  },
  "rule_results": [
    {
      "rule_id": "P002.R1",
      "violated_now": false,
      "feasible_after_action": false,
      "estimated_penalty_delta": 1800,
      "blocked": true,
      "reason": "订单会占用 00:00-01:20，固定休息窗口已无法完整满足"
    }
  ],
  "blocked": true
}
```

这里需要区分：

- `violated_now`：当前是否已经违约。
- `feasible_after_action`：执行候选后，是否仍有机会按时满足规则。
- `estimated_penalty_delta`：候选造成的预计新增罚金。
- `blocked`：策略层是否禁用候选。固定休息、连续休息等 P0 规则可禁用“会造成不可修复违约”的候选。

## 5. 案例一：23 点面对 0 点至 6 点固定休息

### 5.1 DSL

```json
{
  "rule_id": "P002.R1",
  "type": "daily_rest_window",
  "progress_kind": "daily",
  "params": {"window": {"start": "00:00", "end": "06:00"}}
}
```

### 5.2 当前状态

```json
{
  "now": "2026-03-08 23:00:00",
  "next_window": ["2026-03-09 00:00:00", "2026-03-09 06:00:00"],
  "forced_now": false
}
```

当前还没有进入休息窗口，因此可以扫描货源。但规则层必须只保留能够在 `00:00` 前结束的非休息动作。还应给扫描预留安全余量；例如查询 50 条货源预计消耗 5 分钟，则接近 `23:55` 时不再扫描。

### 5.3 扫描后候选

假设扫描结束时为 `23:02`：

| 候选 | 模拟完成时间 | 规则评估 | 结果 |
| --- | --- | --- | --- |
| `order:C101` | `23:48` | 完单后仍可从 `23:48` 休息到 `06:00` | 保留 |
| `order:C102` | `01:20` | 占用固定休息窗口，无法完整休息 6 小时 | 禁用 |
| `wait:418` | `06:00` | 从 `23:02` 直接等待到窗口结束 | 保留 |

若 `C101` 收益合理，可以先接 `C101`。订单在 `23:48` 完成后，下一轮应输出：

```json
{
  "action": "wait",
  "params": {"duration_minutes": 372}
}
```

司机将直接休息到次日 `06:00`。如果没有 `C101` 这种能在午夜前结束的好单，则在 `23:02` 直接选择 `wait:418`。

当时间已经到达 `00:00` 后，forced action 门应在扫描前直接生成：

```json
{
  "action": "wait",
  "params": {"duration_minutes": 360}
}
```

此时不调用 LLM，也不再查询货源。

如果进入窗口后才发现此前已经有非休息动作占用了窗口，例如 `01:00` 时仍未休息，则本日完整窗口要求已经无法修复。此时记录 `missing_elapsed_minutes: 60`，但不再强制等待，让司机重新参与正常动作选择。

## 6. 案例二：19 点面对当天连续休息 5 小时

### 6.1 DSL

```json
{
  "rule_id": "P006.R1",
  "type": "daily_continuous_rest",
  "progress_kind": "daily",
  "params": {"min_minutes": 300}
}
```

### 6.2 历史快照

假设当天早上只零散休息过 60 分钟：

```json
{
  "wait_intervals_by_day": {
    "7": [
      {
        "start_minutes": 10680,
        "end_minutes": 10740,
        "duration_minutes": 60,
        "position": {"lat": 23.10, "lng": 113.30}
      }
    ]
  }
}
```

连续休息不能将多个分散区间相加。早上的 60 分钟不能抵扣晚上的 300 分钟要求。

### 6.3 progress

```json
{
  "rule_id": "P006.R1",
  "type": "daily_continuous_rest",
  "as_of_time": "2026-03-08 19:00:00",
  "day_end": "2026-03-09 00:00:00",
  "required_continuous_minutes": 300,
  "longest_completed_wait_minutes": 60,
  "satisfied": false,
  "latest_rest_start": "2026-03-08 19:00:00",
  "forced_now": true
}
```

计算公式：

```text
latest_rest_start = 当天 24:00 - min_minutes
                  = 24:00 - 300 分钟
                  = 19:00
```

现在已经到达最晚休息时刻。forced action 门必须在扫描前直接输出：

```json
{
  "action": "wait",
  "params": {"duration_minutes": 300}
}
```

司机从 `19:00` 连续休息到 `24:00`，满足当天规则。

### 6.4 为什么不能继续接单

如果仍然扫描并看到一个 `19:40` 完成、净收益 `3000` 的订单，候选投影为：

```json
{
  "candidate_id": "order:C201",
  "simulation": {"action_end_time": "2026-03-08 19:40:00"},
  "rule_results": [
    {
      "rule_id": "P006.R1",
      "feasible_after_action": false,
      "estimated_penalty_delta": 400,
      "blocked": true,
      "reason": "完单后距午夜只剩 260 分钟，无法再形成连续 300 分钟休息"
    }
  ]
}
```

更早一些时仍然可以灵活接单。例如 `18:00` 时接一个 `18:50` 完成的订单，之后从 `18:50` 休息到 `23:50`，规则仍可满足。滚动规划不是提前锁死整天安排，而是在每轮决策时持续计算剩余余量。

## 7. LLM 的位置

LLM 不负责计算 progress，也不负责判断强制休息是否成立。它适合处理安全候选之间的柔性权衡：

```json
{
  "progress_summary": [
    {"rule_id": "P010.R1", "remaining": 2, "slack_days": 4}
  ],
  "candidates": [
    {"candidate_id": "order:C301", "final_score": 1200},
    {"candidate_id": "reposition:增城", "final_score": 1080},
    {"candidate_id": "wait:30", "final_score": 0}
  ]
}
```

LLM 只能返回：

```json
{"candidate_id": "reposition:增城"}
```

固定窗口、最晚休息时刻、订单是否越过窗口等判断由程序完成。LLM 输出非法候选或调用失败时，回退到程序排序最高的安全候选。

## 8. 下一阶段最小实现顺序

1. 建立规则注册表：根据 `rule.type` 找到对应处理器。
2. 先实现 `daily_continuous_rest` 和 `daily_rest_window` 的 progress。
3. 增加预查询 forced action 门，必要时直接输出完整 `wait`。
4. 增加候选投影评估，禁用会造成不可修复休息违约的订单。
5. 扩展等待候选：短等待、补足连续休息、等待到固定窗口结束。

这一阶段不需要接入 LLM。先用确定性规则跑通并验证休息罚分，再将 LLM 放到安全候选的柔性仲裁位置。


DSL 的 type + params
        +
rule_progress
        +
候选动作的模拟结果
        ↓
规则处理器
        ↓
过滤 / 扣分 / 加分 / 强制动作
