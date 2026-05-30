# 司机偏好 DSL 设计草案

## 1. 设计原则

- DSL 只保存偏好的静态语义，不保存实时完成进度。
- `progress_kind` 使用枚举，不使用 `true / false`：
  - `none`：只检查当前候选，不需要轨迹进度。
  - `daily`：按自然日维护状态，每天重置。
  - `monthly_quota`：按月累计次数或天数，并计算剩余额度。
  - `cumulative`：按月累计里程等连续数值。
  - `state_machine`：按顺序推进多个地点或阶段。
- 后续由规则注册表根据 `type` 计算独立的 `rule_progress`。不要把动态字段写回 DSL。
- 原始偏好中的 `start_time / end_time` 是接口可见窗口，不是业务生效时间，不写入 DSL。
- 只有偏好正文明确写出的日期、时段才进入 `active_period` 或规则参数。
- 一个偏好正文可以拆成多个原子规则，共享同一份罚金。例如“23 点前回家，次日 8 点前不出车”拆成地点截止与禁行动作窗口。

## 2. 顶层结构

```json
{
  "preference_id": "P001",
  "source_text": "原始偏好文本",
  "penalty": {
    "mode": "once | per_day | per_order | per_action | per_excess_unit",
    "amount": 100,
    "cap": null,
    "unit": "km"
  },
  "combine": "all",
  "rules": [
    {
      "rule_id": "P001.R1",
      "type": "daily_continuous_rest",
      "progress_kind": "daily",
      "active_period": null,
      "params": {}
    }
  ]
}
```

字段说明：

- `penalty.unit`：仅 `per_excess_unit` 需要，例如 `km` 或 `minute`。
- `combine`：单条规则可省略；复合偏好使用 `all`，表示全部子规则都满足才算满足。
- `active_period`：仅正文明确写出绝对日期范围时填写，否则为 `null`。

## 3. 通用子结构

### Region

```json
{"kind": "named", "name": "惠州"}
```

```json
{"kind": "circle", "lat": 23.30, "lng": 113.52, "radius_km": 20}
```

```json
{"kind": "bbox", "lat_min": 22.42, "lat_max": 22.89, "lng_min": 113.74, "lng_max": 114.66}
```

### ActivePeriod

```json
{"start": "2026-03-04 00:00:00", "end": "2026-03-05 23:59:59"}
```

### DailyWindow

```json
{"start": "23:00", "end": "06:00"}
```

`end` 早于或等于 `start` 时表示跨越午夜。

## 4. 原子规则类型

### 4.1 `daily_continuous_rest`

每天至少有一段连续停车休息。

```json
{
  "type": "daily_continuous_rest",
  "progress_kind": "daily",
  "params": {"min_minutes": 480}
}
```

### 4.2 `daily_rest_window`

每天在指定时段停车休息。与“只禁止接单或空驶”不同，该规则要求完整休息。

```json
{
  "type": "daily_rest_window",
  "progress_kind": "daily",
  "params": {"window": {"start": "00:00", "end": "06:00"}}
}
```

### 4.3 `monthly_off_day_quota`

自然月内至少保留若干完整停驶日。

```json
{
  "type": "monthly_off_day_quota",
  "progress_kind": "monthly_quota",
  "params": {
    "min_days": 3,
    "blocked_actions": ["take_order", "reposition"]
  }
}
```

如果正文只写“不接单”，则 `blocked_actions` 仅填写 `["take_order"]`。

### 4.4 `cargo_category_policy`

禁止接取某些货物品类。

```json
{
  "type": "cargo_category_policy",
  "progress_kind": "none",
  "params": {
    "categories": ["机械设备", "蔬菜"]
  }
}
```

### 4.5 `region_policy`

要求车辆或订单端点位于区域内 / 区域外。

```json
{
  "type": "region_policy",
  "progress_kind": "none",
  "params": {
    "policy": "stay_inside | stay_outside",
    "region": {"kind": "named", "name": "惠州"}
  }
}
```

区域限制默认同时约束车辆当前位置、订单装货点和订单卸货点。任一位置违反限制时，后续统一候选评估器均应判定该动作违反偏好。
- `region.kind` 支持 `named`、`bbox`、`circle`，分别表示命名区域、经纬度矩形和圆形区域。

例如“车辆只能在深圳范围内活动，不出市”：

```json
{
  "type": "region_policy",
  "progress_kind": "none",
  "params": {
    "policy": "stay_inside",
    "region": {
      "kind": "bbox",
      "lat_min": 22.42,
      "lat_max": 22.89,
      "lng_min": 113.74,
      "lng_max": 114.66
    }
  }
}
```

例如“车辆不得进入圆形禁区”：

```json
{
  "type": "region_policy",
  "progress_kind": "none",
  "params": {
    "policy": "stay_outside",
    "region": {"kind": "circle", "lat": 23.30, "lng": 113.52, "radius_km": 20}
  }
}
```

### 4.6 `recurring_action_blackout`

每天指定时段禁止某些动作，但不要求整段显式休息。

```json
{
  "type": "recurring_action_blackout",
  "progress_kind": "daily",
  "params": {
    "window": {"start": "23:00", "end": "06:00"},
    "blocked_actions": ["take_order", "reposition"]
  }
}
```

### 4.7 `distance_limit`

限制单笔或月度距离。

```json
{
  "type": "distance_limit",
  "progress_kind": "none",
  "params": {
    "metric": "pickup_deadhead_km | haul_distance_km",
    "max_km": 90
  }
}
```

月度累计空驶使用：

```json
{
  "type": "distance_limit",
  "progress_kind": "cumulative",
  "params": {
    "metric": "monthly_deadhead_km",
    "max_km": 100
  }
}
```

`monthly_deadhead_km` 只累计接单后赴装货点的 `pickup_deadhead_km` 与主动调度的 `reposition.distance_km`，不累计承运货物时的 `haul_distance_km`。单笔限制不生成 progress；月度累计限制由轨迹历史生成 `current_km`、`remaining_free_km` 和 `exceeded_km`。

### 4.8 `daily_order_limit`

限制每天接单数量。

```json
{
  "type": "daily_order_limit",
  "progress_kind": "daily",
  "params": {"max_orders": 3}
}
```

### 4.9 `daily_first_order_deadline`

当天有接单时，首单必须在指定时间前开始。

```json
{
  "type": "daily_first_order_deadline",
  "progress_kind": "daily",
  "params": {"latest_start": "12:00"}
}
```

### 4.10 `region_visit_quota`

在月内满足目标区域访问额度。可按到达位置统计，也可按订单起终点统计。

```json
{
  "type": "region_visit_quota",
  "progress_kind": "monthly_quota",
  "params": {
    "region": {"kind": "circle", "lat": 23.13, "lng": 113.26, "radius_km": 1},
    "event": "position_arrival | cargo_touch",
    "count_by": "distinct_day | occurrence",
    "min_count": 5
  }
}
```

- `event: position_arrival`：车辆完成接单或主动调度动作后到达目标区域。
- `event: cargo_touch`：已接受订单的装货点或卸货点位于目标区域。主动空驶到区域内不能充数。
- `count_by: distinct_day`：同一天多次访问只计一次。
- `count_by: occurrence`：每次访问分别计数。

运行时 progress 输出 `required_count`、`completed_count`、`remaining_count`、`remaining_days`、`slack_days` 和 `satisfied`。其中 `slack_days` 只对 `distinct_day` 有明确含义；`occurrence` 模式下为 `null`。

轨迹事件允许携带可选的 `cargo_touch_points`，保存订单装卸触点。当前评测历史可以直接使用卸货后的 `position_after` 作为回退事实；如果后续需要完整覆盖“装货点或卸货点”，还需要在执行订单时补充记录装货触点。

### 4.11 `daily_location_deadline`

每天指定时间前到达目标地点。

```json
{
  "type": "daily_location_deadline",
  "progress_kind": "daily",
  "params": {
    "region": {"kind": "circle", "lat": 23.12, "lng": 113.28, "radius_km": 1},
    "arrive_by": "23:00"
  }
}
```

### 4.12 `scheduled_route`

在指定日期窗口内按顺序到达一个或多个地点，可要求停留。

```json
{
  "type": "scheduled_route",
  "progress_kind": "state_machine",
  "params": {
    "steps": [
      {
        "region": {"kind": "circle", "lat": 23.15, "lng": 113.67, "radius_km": 2},
        "window": {"start": "2026-03-31 00:00:00", "end": "2026-03-31 12:00:00"},
        "stay_minutes": 0
      },
      {
        "region": {"kind": "circle", "lat": 23.32, "lng": 112.83, "radius_km": 2},
        "window": {"start": "2026-03-31 00:00:00", "end": "2026-03-31 12:00:00"},
        "stay_minutes": 120
      }
    ]
  }
}
```

单地点停留任务也使用该类型，只填写一个 `step`。

## 5. 复合偏好示例

“每天 23 点前回家，23 点至次日 8 点不接单、不空跑”：

```json
{
  "preference_id": "P009",
  "source_text": "每天23点前车辆须在自家位置一公里内；当天23点至次日8点不接单、不空跑。",
  "penalty": {"mode": "per_day", "amount": 900, "cap": 27000},
  "combine": "all",
  "rules": [
    {
      "rule_id": "P009.R1",
      "type": "daily_location_deadline",
      "progress_kind": "daily",
      "params": {
        "region": {"kind": "circle", "lat": 23.12, "lng": 113.28, "radius_km": 1},
        "arrive_by": "23:00"
      }
    },
    {
      "rule_id": "P009.R2",
      "type": "recurring_action_blackout",
      "progress_kind": "daily",
      "params": {
        "window": {"start": "23:00", "end": "08:00"},
        "blocked_actions": ["take_order", "reposition"]
      }
    }
  ]
}
```

## 6. DSL 与运行时 Progress 的关系

DSL 不应写入动态字段，例如 `satisfied_days`、`remaining_days` 或 `current_km`。这些值会随动作变化，放入 DSL 会污染缓存。

运行时由规则处理器读取 DSL 与 `TrajectoryMemory.snapshot()`，单独生成：

```json
{
  "rule_id": "P001.R1",
  "progress_kind": "monthly_quota",
  "satisfied": 1,
  "required": 3,
  "remaining": 2,
  "slack_days": 5
}
```

处理流程：

```text
偏好文本 + penalty
        ↓ LLM 编译一次并缓存
CompiledPreference DSL
        +
TrajectoryMemory.snapshot()
        ↓ rule registry
rule_progress
        ↓
后续候选过滤、打分、forced action
```

## 7. 样本覆盖清单

除 `drivers2.json` 中明确标记为“临时约定”的两条偏好外，当前样本均可由以上类型表达：

- 连续休息、固定时段休息、整天休息额度。
- 禁接 / 尽量避免货物品类。
- 城市、圆形区域、经纬度边界区域限制。
- 固定时段禁止接单或空驶。
- 单笔赴装货空驶、干线距离、月度累计空驶上限。
- 每日首单截止时间、每日接单数上限。
- 区域访问次数或不同自然日额度。
- 每日回家截止时间与夜间保护窗口。
- 指定日期单地点停留、按顺序多地点任务。
