"""Prompt text for optional preference tools."""

from __future__ import annotations


TOOL_BRIEFS: dict[str, str] = {
    "geo_checks": "用于明确坐标/半径或经纬度范围的地理限制，例如禁入某圆形区域、车辆必须留在某矩形范围内。",
    "candidate_geo_contribution": "用于候选订单是否能贡献一次到访累计，例如自然月内到达某坐标半径内若干天。",
    "history_geo_summary": "用于统计历史中到达某目标区域的自然日次数、今天是否已到访、剩余次数。",
    "time_window_check": "用于固定时间窗口内不接单、不空车赶路、必须停车休息等偏好。",
    "deadline_location_check": "用于必须在某个时间前到达指定坐标半径内的偏好，也可生成受控 deadline reposition。",
    "wait_generation": "用于连续休息、固定休息窗口、月内完全休息天数等偏好触发 wait 候选。",
}


TOOL_DETAILS: dict[str, str] = {
    "geo_checks": (
        "geo_checks 用于判断候选动作是否违反地理范围类偏好，"
        "例如车辆不得进入某个圆形区域、车辆必须始终留在某个经纬度范围内。"
        "配置格式："
        '{"relation":"forbidden_inside|must_inside",'
        '"center":[lat,lng]或null,'
        '"radius_km":数字或null,'
        '"lat_range":[min,max]或null,'
        '"lng_range":[min,max]或null,'
        '"reason":"事实说明"}。'
        "字段含义："
        "relation 表示地理约束关系。"
        "forbidden_inside 表示车辆不得进入该区域，例如“不得进入以某点为圆心、半径20公里的区域”。"
        "must_inside 表示车辆必须保持在该区域内，例如“车辆位置须始终在深圳市经纬度范围内”。"
        "如果偏好描述的是圆形区域，填写 center 和 radius_km，lat_range/lng_range 必须为 null。"
        "如果偏好描述的是经纬度矩形范围，填写 lat_range 和 lng_range，center/radius_km 必须为 null。"
        "center+radius_km 与 lat_range+lng_range 只能二选一。"
        "reason 用于解释该地理约束的来源，例如禁入区域、必须在区域内等。"
    ),
    "candidate_geo_contribution": (
        "candidate_geo_contribution 用于判断候选订单是否有助于完成“到达某地点/区域若干次”的累计偏好。"
        "它不用于判断禁入区域，也不直接统计历史次数，只判断当前模拟订单是否能贡献一次到访。"
        "配置格式："
        '{"relation":"must_visit",'
        '"center":[lat,lng]或null,'
        '"radius_km":数字或null,'
        '"lat_range":[lat_min,lat_max]或null,'
        '"lng_range":[lng_min,lng_max]或null,'
        '"reason":"说明需要判断候选订单是否能到达偏好要求目标区域"}。'
        "relation 固定为 must_visit。"
        "如果目标是圆形区域，填写 center 和 radius_km，lat_range/lng_range 为 null。"
        "如果目标是经纬度矩形范围，填写 lat_range 和 lng_range，center/radius_km 为 null。"
        "若启用它处理累计到访偏好，通常也要启用 history_geo_summary。"
    ),
    "history_geo_summary": (
        "history_geo_summary 用于统计历史上已经完成了多少次/多少天目标地点到访，"
        "常用于“自然月内至少N个不同自然日到过某地点附近”这类累计到访偏好。"
        "它不判断单个候选订单是否能到达目标地点；单个候选订单贡献由 candidate_geo_contribution 判断。"
        "配置格式："
        '{"reason":"说明需要统计什么周期内的目标地点到访次数",'
        '"required_visit_count":N,'
        '"period":"current_month",'
        '"count_unit":"distinct_day"}。'
        "字段含义："
        "required_visit_count 表示偏好要求到达目标地点的次数或天数，例如至少5个不同自然日则填5。"
        "period 表示统计周期；当前只支持 current_month，表示当前自然月。"
        "count_unit 表示计数方式；distinct_day 表示按不同自然日去重统计，同一天多次到达只算1次。"
        "reason 只用于解释和兜底，不要把 period、required_visit_count 等关键信息只写在 reason 里，"
        "必须同时填写到对应字段。"
        "如果启用 history_geo_summary，通常必须同时启用 candidate_geo_contribution，"
        "并让 candidate_geo_contribution 描述同一个目标区域。"
    ),
    "time_window_check": (
        "time_window_check 只用于固定时间窗口休息/禁行偏好，"
        "例如“每天23点至次日4点不接单、不空车赶路”或“中午12点至13点休息”。"
        "它用于判断模拟 take_order 或 reposition 的执行时间区间是否与固定禁止动作窗口重叠。"
        "配置格式："
        '{"start":"HH:MM","end":"HH:MM","cross_day":true或false,"reason":"固定窗口说明"}。'
        "如果窗口是“23点至次日4点”，cross_day=true。"
        "如果窗口是“12点至13点”，cross_day=false。"
    ),
    "deadline_location_check": (
        "deadline_location_check 用于判断模拟接单完成后，司机是否还能在规定时间前到达指定地点。"
        "例如“晚上23点前回家”或“某时间前必须到达某坐标附近”。"
        "它会考虑候选订单完成时间、完成位置，以及从完成位置空驶到目标地点所需时间。"
        "配置格式："
        '{"center":[lat,lng],"radius_km":数字,"deadline_time":"HH:MM","reason":"期限地点说明"}。'
        "center 表示 deadline 目标地点坐标。"
        "radius_km 表示到达目标地点的容忍半径；如果原文给了坐标但没有给半径，默认填 1。"
        "deadline_time 表示必须到达目标地点的每日时间，例如 23:00。"
    ),
    "wait_generation": (
        "wait_generation 用于根据休息类偏好生成 wait 候选动作。"
        "它只生成候选 wait 动作，不判断订单是否违规。"
        "配置格式："
        '{"continuous_rest":{"hours":数字,"weekdays_only":true或false}或null,'
        '"fixed_rest_window":{"start":"HH:MM","end":"HH:MM","cross_day":true或false}或null,'
        '"monthly_rest_days":{"days":数字}或null,'
        '"reason":"生成wait候选的原因"}。'
        "三个字段的区别："
        "continuous_rest 表示每日/平日需要连续休息若干小时，例如“每天连续休息4小时”。"
        "fixed_rest_window 表示固定时间窗口内应休息或不接单不空跑，例如“每天23点至次日4点休息”。"
        "monthly_rest_days 表示自然月内需要完整休息若干天，例如“自然月内至少2天完全歇着”。"
        "continuous_rest.weekdays_only 只在原文明确写“平日”“工作日”“非周末”时为 true。"
        "如果原文写“每天”“每日”“每晚”，weekdays_only 必须为 false。"
        "如果某类休息偏好不存在，对应字段填 null。"
    ),
}


def brief_tool_intro(tool_names: tuple[str, ...]) -> str:
    lines = ["可用工具仅用于生成事实证据，不直接决定罚分或动作："]
    for name in tool_names:
        brief = TOOL_BRIEFS.get(name)
        if brief:
            lines.append(f"- {name}: {brief}")
    return "\n".join(lines)


def detailed_tool_prompt(tool_names: list[str]) -> str:
    return "\n".join(TOOL_DETAILS[name] for name in tool_names if name in TOOL_DETAILS)
