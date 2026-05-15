# Agent 决策新增变量说明

本文解释 Agent 在构造 `cargo_candidates` 时新增的辅助变量。这些变量只用于决策提示和后续筛选评分，不改变仿真引擎的真实执行规则。

## 时间基准

- `simulation_progress_minutes`：自 `2026-03-01 00:00:00` 起经过的仿真分钟数。
- 本文所有 `*_minutes` 字段均为仿真分钟数或分钟时长。
- 本文所有 `*_time` 字段均为墙钟展示时间，格式为 `YYYY-MM-DD HH:MM:SS`。

## 候选货物增强字段

### `cargo_name`

货源品类名称，来自 `query_cargo` 返回货源的 `cargo.cargo_name`。

用途：用于和偏好分类中的禁接品类、尽量避免品类做匹配。

### `pickup_deadhead_km`

司机当前位置到候选货物装货点的空驶距离，单位 km。

计算口径：优先使用 `query_cargo` 返回的 `distance_km`；若缺失，则用当前位置和货源 `start` 坐标按 Haversine 大圆距离计算。

### `pickup_minutes`

司机从当前位置开到装货点预计需要的分钟数。

计算口径：

- 若 `pickup_deadhead_km <= 1e-6`，视为已在装货点，耗时为 `0`。
- 否则 `ceil(pickup_deadhead_km / speed_km_per_hour * 60)`，且最少为 `1` 分钟。

### `haul_distance_km`

候选货物从装货点到卸货点的直线大圆距离，单位 km。

计算口径：用货源 `start` 和 `end` 坐标按 Haversine 大圆距离计算。

用途：给距离偏好、收益估算和长短途判断提供统一字段。

### `arrival_minutes` / `arrival_time`

预计到达装货点的仿真时刻和墙钟时间。

计算公式：

```text
arrival_minutes = 当前扫描后 simulation_progress_minutes + pickup_minutes
```

### `waiting_minutes`

若司机早于 `load_time` 开始到达装货点，需要等待到装货窗口开始的分钟数。

计算口径：

- 无 `load_time` 时为 `0`。
- 有 `load_time` 且未错过窗口时，为 `max(0, load_start_minutes - arrival_minutes)`。
- 已错过装货窗口时为 `0`，并由 `load_time_missed=true` 表示不可赶上。

### `finish_minutes` / `finish_time`

预计完成该单后的仿真时刻和墙钟时间。

计算公式：

```text
finish_minutes = arrival_minutes + waiting_minutes + cost_time_minutes
```

若已经错过装货窗口，则该字段为 `null`。

### `estimated_total_minutes`

预计执行该订单的总耗时，单位分钟。

计算公式：

```text
estimated_total_minutes = pickup_minutes + waiting_minutes + cost_time_minutes
```

若已经错过装货窗口，则该字段为 `null`。

### `load_time_missed`

是否预计无法赶上装货时间窗。

判断口径：若存在 `load_time`，且 `arrival_minutes > load_end_minutes`，则为 `true`。

### `expired_after_scan`

扫描结束后，该货源是否已经超过 `remove_time`。

判断口径：若 `扫描后的 simulation_progress_minutes > remove_minutes`，则为 `true`。

### `remove_minutes`

货源 `remove_time` 转换成的仿真分钟数。

用途：辅助判断 `expired_after_scan`，也方便后续过滤或日志排查直接用分钟口径比较。

### `income_eligible`

在已知仿真总时长时，预计该候选单完成后是否仍在仿真周期内。

取值口径：

- 若当前环境未传入仿真总时长，或该候选已经错过装货窗口，则为 `null`。
- 否则 `finish_minutes <= simulation_horizon_minutes` 时为 `true`，反之为 `false`。

注意：该字段只表示收益周期判断的辅助信息，不代表 Agent 已经筛掉该候选。

## 关于 `truck_length`

本次比赛规则已简化：计算得分和判断能否运输货物时不考虑 `truck_length`。因此 Agent 可以保留货源原始 `truck_length` 字段用于展示，但不应基于该字段做筛选、评分或风险判断。
