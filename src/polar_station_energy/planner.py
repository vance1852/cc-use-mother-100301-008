"""站内能源承诺排程引擎：把发电、储能、燃料批次与分时需求连成确定性预测。

排程器只依赖纯数据结构，不接触数据库，便于离线重放与审计复核：

- 时间被切分为固定长度的时段（slot），所有决策都落在时段边界上，时钟由调用方控制；
- 保障等级为 medical / comms / antifreeze 的回路在任何情况下都保持供电；
- 成组回路整组投切，绝不把一组设备切成部分供电的危险状态；
- 回路依赖向上闭包：被保留的回路会带上它依赖的回路，依赖失效时随动切除；
- 设备一旦投入必须满足最低运行时长，OFF→ON 切换计入启动代价；
- 实验回路只在获批窗口内允许供电；
- 燃料按批次可用时段累计，并做全时程配给：先守住保障负荷的燃料储备，
  再按保留优先级接纳一般负荷，燃料未到的批次不会提前支取。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SLOT_MINUTES = 30
SLOT_HOURS = SLOT_MINUTES / 60.0
EPS = 1e-9

MUST_TIERS = ("medical", "comms", "antifreeze")
TIERS = MUST_TIERS + ("life_support", "experiment", "comfort", "general")

# 指令依据编码，工程师据此核验每个回路保留或切除的原因
REASON_TIER_MUST = "tier_must_keep"
REASON_DEPENDENCY = "dependency_required"
REASON_GROUP = "group_required"
REASON_OVERRIDE_ON = "override_forced_on"
REASON_OVERRIDE_OFF = "override_forced_off"
REASON_MIN_RUN = "min_run_active"
REASON_WINDOW = "experiment_window"
REASON_KEPT = "kept_within_budget"
REASON_SHED_DEFICIT = "shed_deficit"
REASON_SHED_FUEL = "shed_fuel_reserve"
REASON_SHED_DEPENDENCY = "shed_dependency"
REASON_SHED_GROUP = "shed_group"
REASON_OUTSIDE_WINDOW = "outside_window"
REASON_ASSET_FAILED = "asset_failed"

SHED_REASONS = (
    REASON_SHED_DEFICIT,
    REASON_SHED_FUEL,
    REASON_SHED_DEPENDENCY,
    REASON_SHED_GROUP,
)


@dataclass(frozen=True)
class CircuitSpec:
    """配电回路的排程视图。"""

    circuit_id: str
    tier: str
    default_kw: float
    demand_overrides: dict[int, float]
    group_id: str | None
    depends_on: tuple[str, ...]
    min_run_slots: int
    startup_kwh: float
    shed_rank: int
    failed_from_slot: int | None = None


@dataclass(frozen=True)
class GeneratorSpec:
    """柴油发电机的排程视图。"""

    generator_id: str
    rated_kw: float
    min_kw: float
    liters_per_kwh: float
    failed_from_slot: int | None = None


@dataclass(frozen=True)
class BatterySpec:
    """储能设备的排程视图，start_soc_kwh 为排程起点时的实测电量。"""

    battery_id: str
    capacity_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    efficiency: float
    start_soc_kwh: float
    failed_from_slot: int | None = None


@dataclass(frozen=True)
class FuelBatchSpec:
    """燃料批次的排程视图，available_slot 之前不可支取。"""

    batch_id: str
    remaining_liters: float
    available_slot: int


@dataclass(frozen=True)
class PlanInput:
    """一次排程的全部输入。"""

    start_slot: int
    horizon: int
    slot_hours: float
    circuits: tuple[CircuitSpec, ...]
    generators: tuple[GeneratorSpec, ...]
    batteries: tuple[BatterySpec, ...]
    fuel_batches: tuple[FuelBatchSpec, ...]
    wind_kw: dict[int, float]
    windows: dict[str, tuple[tuple[int, int], ...]]
    overrides: dict[tuple[str, int], str]
    prior_on: dict[str, bool]
    on_since: dict[str, int]


@dataclass(frozen=True)
class DirectiveOut:
    """一个回路在一个时段的投切指令与依据。"""

    circuit_id: str
    slot: int
    on: bool
    reason: str
    detail: str


@dataclass(frozen=True)
class DispatchOut:
    """一个时段的功率、燃料与储能调度结果。"""

    slot: int
    demand_kw: float
    supply_kw: float
    gen_kw: float
    wind_kw: float
    battery_kw: float
    soc_kwh: float
    fuel_liters: float
    unserved_kw: float
    breach: bool


@dataclass(frozen=True)
class ShedOut:
    """一次负荷切除记录。"""

    circuit_id: str
    slot: int
    reason: str
    detail: str


@dataclass(frozen=True)
class PlanOutput:
    """排程结果：逐回路指令、逐时段调度、切除清单与保障失守标记。"""

    directives: tuple[DirectiveOut, ...]
    dispatch: tuple[DispatchOut, ...]
    sheds: tuple[ShedOut, ...]
    breached: bool


def run_plan(inp: PlanInput) -> PlanOutput:
    """从 start_slot 起排 horizon 个时段，返回确定性的承诺方案。"""

    circuits = {c.circuit_id: c for c in inp.circuits}
    groups: dict[str, list[str]] = {}
    for c in inp.circuits:
        if c.group_id:
            groups.setdefault(c.group_id, []).append(c.circuit_id)

    end_slot = inp.start_slot + inp.horizon
    hours = inp.slot_hours

    def demand_of(cid: str, slot: int) -> float:
        c = circuits[cid]
        return float(c.demand_overrides.get(slot, c.default_kw))

    def wind_of(slot: int) -> float:
        return max(0.0, float(inp.wind_kw.get(slot, 0.0)))

    def is_allowed(cid: str, slot: int) -> bool:
        c = circuits[cid]
        if c.failed_from_slot is not None and slot >= c.failed_from_slot:
            return False
        if inp.overrides.get((cid, slot)) == "off":
            return False
        if c.tier == "experiment":
            wins = inp.windows.get(cid, ())
            if not any(a <= slot < b for a, b in wins):
                return False
        return True

    def closure(ids) -> set[str]:
        """依赖与成组的不动点闭包。"""

        seen = set(ids)
        changed = True
        while changed:
            changed = False
            for cid in list(seen):
                c = circuits[cid]
                for dep in c.depends_on:
                    if dep not in seen:
                        seen.add(dep)
                        changed = True
                if c.group_id:
                    for member in groups[c.group_id]:
                        if member not in seen:
                            seen.add(member)
                            changed = True
        return seen

    must_ids = tuple(c.circuit_id for c in inp.circuits if c.tier in MUST_TIERS)
    must_demand = {s: sum(demand_of(m, s) for m in must_ids) for s in range(inp.start_slot, end_slot)}
    must_net = {s: max(0.0, must_demand[s] - wind_of(s)) * hours for s in range(inp.start_slot, end_slot)}

    gens = list(inp.generators)
    bats = list(inp.batteries)
    # 燃料折算保守取最大油耗，保证保障负荷的储备估计不偏乐观
    fleet_rate = max((g.liters_per_kwh for g in gens), default=0.0)

    initial_liters = {f.batch_id: max(0.0, f.remaining_liters) for f in inp.fuel_batches}
    batch_slot = {f.batch_id: f.available_slot for f in inp.fuel_batches}
    fuel_remaining = dict(initial_liters)

    def liters_to_kwh(liters: float) -> float:
        return liters / fleet_rate if fleet_rate > EPS else 0.0

    # 燃料配给：对每个时段 s 计算“累计可用燃料 − 累计保障需求”，再取后缀最小值，
    # 得到一般负荷在 s 之前允许消耗的累计能量上限。这样下一批燃料到达之前，
    # 医疗、通信与防冻负荷的燃料储备不会被一般负荷透支。
    cum_fuel: dict[int, float] = {}
    cum_must_through: dict[int, float] = {}
    acc_must = 0.0
    for s in range(inp.start_slot, end_slot):
        cum_fuel[s] = liters_to_kwh(sum(v for b, v in initial_liters.items() if batch_slot[b] <= s))
        acc_must += must_net[s]
        cum_must_through[s] = acc_must
    optional_margin: dict[int, float] = {}
    best = math.inf
    for s in range(end_slot - 1, inp.start_slot - 1, -1):
        best = min(best, cum_fuel[s] - cum_must_through[s])
        optional_margin[s] = best

    soc = {b.battery_id: min(max(0.0, b.start_soc_kwh), b.capacity_kwh) for b in bats}
    state = {c.circuit_id: bool(inp.prior_on.get(c.circuit_id, c.tier in MUST_TIERS)) for c in inp.circuits}
    on_since = dict(inp.on_since)

    directives: list[DirectiveOut] = []
    dispatch: list[DispatchOut] = []
    sheds: dict[tuple[str, int], ShedOut] = {}
    breached = False
    cum_optional = 0.0

    for slot in range(inp.start_slot, end_slot):
        wind = wind_of(slot)
        avail_gens = [g for g in gens if g.failed_from_slot is None or slot < g.failed_from_slot]
        avail_bats = [b for b in bats if b.failed_from_slot is None or slot < b.failed_from_slot]
        gen_cap = sum(g.rated_kw for g in avail_gens)
        fuel_now_liters = sum(v for b, v in fuel_remaining.items() if batch_slot[b] <= slot)
        fuel_now_kwh = liters_to_kwh(fuel_now_liters)

        on: dict[str, str] = {}
        detail: dict[str, str] = {}

        # 保障回路：医疗、通信、防冻必须供电
        for cid in must_ids:
            c = circuits[cid]
            if c.failed_from_slot is not None and slot >= c.failed_from_slot:
                continue
            on[cid] = REASON_TIER_MUST
            detail[cid] = f"保障等级 {c.tier} 必须供电"
        # 人工覆盖（双人确认且在期限内）强制供电，但不能突破故障与获批窗口
        for (cid, s), desired in inp.overrides.items():
            if s != slot or desired != "on" or cid not in circuits or cid in on:
                continue
            if is_allowed(cid, slot):
                on[cid] = REASON_OVERRIDE_ON
                detail[cid] = "人工覆盖（双人确认）强制供电"
        # 最低运行时长：已投入的设备必须运行满约定时段
        for c in inp.circuits:
            cid = c.circuit_id
            if cid in on or not c.min_run_slots or not state.get(cid):
                continue
            since = on_since.get(cid)
            if since is not None and slot < since + c.min_run_slots and is_allowed(cid, slot):
                on[cid] = REASON_MIN_RUN
                detail[cid] = f"最低运行时长剩余 {since + c.min_run_slots - slot} 个时段"
        # 依赖与成组闭包
        changed = True
        while changed:
            changed = False
            for cid in list(on):
                c = circuits[cid]
                for dep in c.depends_on:
                    if dep not in on and is_allowed(dep, slot):
                        on[dep] = REASON_DEPENDENCY
                        detail[dep] = f"回路 {cid} 依赖其供电"
                        changed = True
                if c.group_id:
                    for member in groups[c.group_id]:
                        if member not in on and is_allowed(member, slot):
                            on[member] = REASON_GROUP
                            detail[member] = f"与回路 {cid} 同属组 {c.group_id}，整组投切"
                            changed = True
        # 组完整性：成员不可用时整组退出，避免部分供电的危险状态
        for gid, members in groups.items():
            present = [m for m in members if m in on]
            if present and any(not is_allowed(m, slot) for m in members):
                for m in members:
                    if m in on and circuits[m].tier not in MUST_TIERS:
                        del on[m]
                        sheds[(m, slot)] = ShedOut(m, slot, REASON_SHED_GROUP, f"组 {gid} 成员不可用，整组退出")
        # 依赖完整性：依赖未供电的回路随动退出（保障回路除外，登记时不允许成组）
        changed = True
        while changed:
            changed = False
            for cid in list(on):
                if on[cid] == REASON_TIER_MUST:
                    continue
                for dep in circuits[cid].depends_on:
                    if dep not in on:
                        del on[cid]
                        sheds[(cid, slot)] = ShedOut(cid, slot, REASON_SHED_DEPENDENCY, f"依赖回路 {dep} 未供电")
                        changed = True
                        break

        def startup_kw(cid: str) -> float:
            c = circuits[cid]
            if c.startup_kwh > EPS and not state.get(cid):
                return c.startup_kwh / hours
            return 0.0

        def demand_now(ids) -> float:
            return sum(demand_of(cid, slot) + startup_kw(cid) for cid in ids)

        demand = demand_now(on.keys())

        def discharge_cap_kw() -> float:
            total = 0.0
            for b in avail_bats:
                total += min(b.max_discharge_kw, max(0.0, soc[b.battery_id]) * b.efficiency / hours)
            return total

        supply_cap = wind + min(gen_cap, fuel_now_kwh / hours) + discharge_cap_kw()

        # 候选单元：成组整体或单个回路，按保留优先级从高到低尝试
        units: list[tuple[str, ...]] = []
        grouped_members: set[str] = set()
        for gid, members in groups.items():
            if any(m not in on for m in members):
                units.append(tuple(sorted(members)))
            grouped_members.update(members)
        for c in inp.circuits:
            if c.circuit_id not in on and c.circuit_id not in grouped_members:
                units.append((c.circuit_id,))
        units.sort(key=lambda u: (-max(circuits[m].shed_rank for m in u), u[0]))

        for unit in units:
            members = tuple(m for m in unit if m not in on)
            if not members:
                continue
            if any(not is_allowed(m, slot) for m in members):
                if len(members) > 1:
                    blocker = next(m for m in members if not is_allowed(m, slot))
                    for m in members:
                        sheds.setdefault((m, slot), ShedOut(
                            m, slot, REASON_SHED_GROUP, f"组内回路 {blocker} 不可用，整组退出"))
                continue
            required = closure(members) - set(on)
            if any(not is_allowed(r, slot) for r in required):
                blocker = next(r for r in sorted(required) if not is_allowed(r, slot))
                for m in members:
                    same_group = circuits[blocker].group_id and circuits[blocker].group_id == circuits[m].group_id
                    reason = REASON_SHED_GROUP if same_group else REASON_SHED_DEPENDENCY
                    sheds.setdefault((m, slot), ShedOut(m, slot, reason, f"回路 {blocker} 不可用"))
                continue
            add_kw = demand_now(required)
            new_demand = demand + add_kw
            # 燃料配给优先判定：一般负荷的累计消耗不得透支保障负荷的燃料储备
            optional_this = add_kw * hours
            if cum_optional + optional_this > optional_margin[slot] + 1e-6:
                for m in members:
                    sheds.setdefault((m, slot), ShedOut(
                        m, slot, REASON_SHED_FUEL,
                        "为守住医疗、通信与防冻负荷的燃料储备而切除"))
                continue
            if new_demand - supply_cap > 1e-6:
                for m in members:
                    sheds.setdefault((m, slot), ShedOut(
                        m, slot, REASON_SHED_DEFICIT,
                        f"供给上限 {supply_cap:.2f} kW 无法承担 {new_demand:.2f} kW"))
                continue
            for r in sorted(required):
                if r in members:
                    if circuits[r].tier == "experiment":
                        on[r] = REASON_WINDOW
                        detail[r] = "获批实验窗口内保留"
                    else:
                        on[r] = REASON_KEPT
                        detail[r] = "需求在功率与燃料预算内"
                else:
                    c = circuits[r]
                    if c.group_id and any(m in members for m in groups[c.group_id]):
                        on[r] = REASON_GROUP
                        detail[r] = f"与组 {c.group_id} 整组投切"
                    else:
                        on[r] = REASON_DEPENDENCY
                        detail[r] = "被保留回路依赖"
                if startup_kw(r) > EPS:
                    detail[r] += f"（含启动代价 {circuits[r].startup_kwh:g} kWh）"
            demand = new_demand
            for r in required:
                sheds.pop((r, slot), None)

        # 保障回路自身故障的缺口计入失守
        must_failed_kw = sum(
            demand_of(m, slot) for m in must_ids
            if circuits[m].failed_from_slot is not None and slot >= circuits[m].failed_from_slot)

        # 逐时段调度：风能优先，发电机受燃料约束，储能补缺与吸纳剩余
        demand = demand_now(on.keys())
        wind_used = min(wind, demand)
        net = demand - wind_used
        fuel_left = fuel_now_liters
        gen_kw = 0.0
        fuel_liters = 0.0
        remaining_want = net
        for g in avail_gens:
            if remaining_want <= EPS:
                break
            alloc = min(remaining_want, g.rated_kw)
            if EPS < alloc < g.min_kw:
                alloc = min(g.min_kw, g.rated_kw)
            liters = alloc * hours * g.liters_per_kwh
            if liters > fuel_left + EPS:
                alloc = fuel_left / (hours * g.liters_per_kwh) if g.liters_per_kwh > EPS else 0.0
                liters = max(0.0, alloc) * hours * g.liters_per_kwh
            alloc = max(0.0, alloc)
            gen_kw += alloc
            fuel_liters += liters
            fuel_left = max(0.0, fuel_left - liters)
            remaining_want -= alloc
        deficit = net - gen_kw
        batt_dis = 0.0
        if deficit > EPS:
            for b in avail_bats:
                cap_kw = min(b.max_discharge_kw, max(0.0, soc[b.battery_id]) * b.efficiency / hours)
                use = min(deficit - batt_dis, cap_kw)
                if use <= EPS:
                    continue
                soc[b.battery_id] -= use * hours / b.efficiency
                batt_dis += use
        unserved = max(0.0, deficit - batt_dis) + must_failed_kw
        surplus = gen_kw + wind_used - demand
        batt_chg = 0.0
        if surplus > EPS:
            for b in avail_bats:
                room = max(0.0, b.capacity_kwh - soc[b.battery_id])
                cap_kw = min(b.max_charge_kw, room / (b.efficiency * hours))
                use = min(surplus - batt_chg, cap_kw)
                if use <= EPS:
                    continue
                soc[b.battery_id] += use * hours * b.efficiency
                batt_chg += use
        # 燃料批次按可用时段先后支取
        left = fuel_liters
        for bid in sorted((b for b in fuel_remaining if batch_slot[b] <= slot),
                          key=lambda b: (batch_slot[b], b)):
            take = min(fuel_remaining[bid], left)
            fuel_remaining[bid] -= take
            left -= take
            if left <= EPS:
                break
        breach_slot = unserved > 1e-6
        breached = breached or breach_slot
        dispatch.append(DispatchOut(
            slot=slot, demand_kw=demand, supply_kw=gen_kw + wind_used + batt_dis,
            gen_kw=gen_kw, wind_kw=wind_used, battery_kw=batt_dis - batt_chg,
            soc_kwh=sum(soc.values()), fuel_liters=fuel_liters,
            unserved_kw=unserved, breach=breach_slot))

        # 每个回路每个时段都留下保留或切除的依据
        for c in inp.circuits:
            cid = c.circuit_id
            if cid in on:
                directives.append(DirectiveOut(cid, slot, True, on[cid], detail[cid]))
                continue
            if c.failed_from_slot is not None and slot >= c.failed_from_slot:
                directives.append(DirectiveOut(cid, slot, False, REASON_ASSET_FAILED, "设备故障退出运行"))
                continue
            if inp.overrides.get((cid, slot)) == "off":
                directives.append(DirectiveOut(cid, slot, False, REASON_OVERRIDE_OFF, "人工覆盖（双人确认）强制切除"))
                continue
            if c.tier == "experiment" and not any(
                    a <= slot < b for a, b in inp.windows.get(cid, ())):
                directives.append(DirectiveOut(cid, slot, False, REASON_OUTSIDE_WINDOW, "不在获批实验窗口内"))
                continue
            shed = sheds.get((cid, slot))
            if shed is not None:
                directives.append(DirectiveOut(cid, slot, False, shed.reason, shed.detail))
                continue
            blocker = next((d for d in c.depends_on if d not in on), None)
            if blocker is not None:
                directives.append(DirectiveOut(cid, slot, False, REASON_SHED_DEPENDENCY, f"依赖回路 {blocker} 未供电"))
                continue
            directives.append(DirectiveOut(cid, slot, False, REASON_SHED_DEFICIT, "未被纳入供电计划"))

        # 能量累计与投切状态滚动，供下一时段的燃料配给与最低运行时长判断
        optional_kw = sum(
            demand_of(cid, slot) + startup_kw(cid) for cid in on if cid not in must_ids)
        wind_left = max(0.0, wind - must_demand[slot])
        cum_optional += max(0.0, optional_kw - wind_left) * hours
        for c in inp.circuits:
            cid = c.circuit_id
            now_on = cid in on
            if now_on and not state[cid]:
                on_since[cid] = slot
            elif not now_on:
                on_since.pop(cid, None)
            state[cid] = now_on

    return PlanOutput(
        directives=tuple(directives),
        dispatch=tuple(dispatch),
        sheds=tuple(sheds.values()),
        breached=breached,
    )
