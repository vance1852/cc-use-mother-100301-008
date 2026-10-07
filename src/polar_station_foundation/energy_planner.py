"""纯函数能源预测与负荷处置规划器。

把发电能力、储能状态、燃料批次可用量和分时需求连成逐区间预测，
在供应缺口下按保障等级、回路依赖、最低运行时长、启动代价、
获批实验窗口和人工覆盖生成每个回路的保留或切除决定及依据。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping


PROTECTED_REASONS = {1: "protected_medical", 2: "protected_comms", 3: "protected_antifreeze"}
PROTECTED_DETAILS = {
    1: "医疗负荷属于最高保障等级，必须保持供电",
    2: "通信负荷属于保障等级，必须保持供电",
    3: "防冻回路属于保障等级，切除会造成冻损，必须保持供电",
}
EPSILON = 1e-9


@dataclass(frozen=True)
class GeneratorSpec:
    """一台发电机的规划输入。"""

    generator_id: str
    rated_kw: float
    fuel_type: str
    liters_per_kwh: float
    online: bool


@dataclass(frozen=True)
class BatterySpec:
    """站内储能的规划输入（多组已聚合）。"""

    capacity_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    soc_kwh: float
    efficiency: float


@dataclass(frozen=True)
class FuelBatchSpec:
    """一个燃料批次的规划输入。"""

    batch_id: str
    fuel_type: str
    remaining_liters: float
    available_from: datetime


@dataclass(frozen=True)
class CircuitSpec:
    """一个配电回路的规划输入。"""

    circuit_id: str
    name: str
    protection_level: int
    demand_kw: float
    profile: tuple[dict, ...]
    depends_on: str | None
    group_id: str | None
    min_run_intervals: int
    startup_kwh: float
    state: str
    on_since: datetime | None


@dataclass(frozen=True)
class WindowSpec:
    """一个获批实验窗口的规划输入。"""

    window_id: str
    circuit_id: str
    start: datetime
    end: datetime
    required_kw: float


@dataclass(frozen=True)
class OverrideSpec:
    """一条已双人确认人工覆盖的规划输入。"""

    override_id: str
    circuit_id: str
    action: str
    valid_from: datetime
    valid_until: datetime


@dataclass(frozen=True)
class Decision:
    """单个回路在单个区间内的处置决定与依据。"""

    interval_index: int
    interval_start: datetime
    circuit_id: str
    on: bool
    reason_code: str
    reason_detail: str


@dataclass(frozen=True)
class IntervalReport:
    """单个区间的资源预测结果。"""

    index: int
    start: datetime
    demand_kwh: float
    generation_kwh: float
    charge_kwh: float
    discharge_kwh: float
    soc_kwh: float
    fuel_liters: float
    deficit_kwh: float


@dataclass(frozen=True)
class PlanResult:
    """一次完整预测的决定、区间资源和告警。"""

    decisions: tuple[Decision, ...]
    intervals: tuple[IntervalReport, ...]
    alarms: tuple[str, ...]


class _Unit:
    """调度最小单位：单个回路或一个必须同状态的成组回路。"""

    def __init__(self, key: str, members: list[CircuitSpec]) -> None:
        self.key = key
        self.members = members
        self.level = min(member.protection_level for member in members)
        self.min_run = max(member.min_run_intervals for member in members)
        self.startup_kwh = sum(member.startup_kwh for member in members)
        self.parents: set[str] = set()

    def demand_kw(self, when: datetime, windows: list[WindowSpec]) -> float:
        total = 0.0
        for member in self.members:
            kw = _profile_kw(member, when)
            for window in windows:
                if window.circuit_id == member.circuit_id and window.start <= when < window.end:
                    kw = max(kw, window.required_kw)
            total += kw
        return total


def _profile_kw(circuit: CircuitSpec, when: datetime) -> float:
    """按分时需求曲线取当前小时的功率，没有命中则用基础功率。"""

    hour = when.hour + when.minute / 60.0
    for entry in circuit.profile:
        start = float(entry["start_hour"])
        end = float(entry["end_hour"])
        if start <= end:
            matched = start <= hour < end
        else:
            matched = hour >= start or hour < end
        if matched:
            return float(entry["kw"])
    return circuit.demand_kw


def _build_units(circuits: list[CircuitSpec]) -> dict[str, _Unit]:
    groups: dict[str, list[CircuitSpec]] = {}
    for circuit in circuits:
        key = circuit.group_id or circuit.circuit_id
        groups.setdefault(key, []).append(circuit)
    units = {key: _Unit(key, members) for key, members in groups.items()}
    circuit_unit = {}
    for key, unit in units.items():
        for member in unit.members:
            circuit_unit[member.circuit_id] = key
    for key, unit in units.items():
        for member in unit.members:
            if member.depends_on and member.depends_on in circuit_unit:
                parent = circuit_unit[member.depends_on]
                if parent != key:
                    unit.parents.add(parent)
    return units


def _ancestors(units: dict[str, _Unit], key: str) -> set[str]:
    seen: set[str] = set()
    stack = list(units[key].parents)
    while stack:
        current = stack.pop()
        if current in seen or current not in units:
            continue
        seen.add(current)
        stack.extend(units[current].parents)
    return seen


def _descendants(units: dict[str, _Unit], key: str) -> set[str]:
    children: dict[str, set[str]] = {}
    for unit_key, unit in units.items():
        for parent in unit.parents:
            children.setdefault(parent, set()).add(unit_key)
    seen: set[str] = set()
    stack = [key]
    while stack:
        current = stack.pop()
        for child in children.get(current, ()):  # noqa: B007
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def compute_plan(
    *,
    horizon_start: datetime,
    interval_minutes: int,
    intervals: int,
    generators: list[GeneratorSpec],
    battery: BatterySpec | None,
    fuel_batches: list[FuelBatchSpec],
    circuits: list[CircuitSpec],
    windows: list[WindowSpec],
    overrides: list[OverrideSpec],
    locked: Mapping[int, Mapping[str, tuple[bool, str, str]]] | None = None,
) -> PlanResult:
    """逐区间模拟发电、储能、燃料与负荷处置，返回决定与资源预测。

    locked 中列出的区间沿用既有决定（已经执行的指令保持原样），
    其余区间按当前供应与约束重新计算。
    """

    delta = timedelta(minutes=interval_minutes)
    dt_hours = interval_minutes / 60.0
    locked = locked or {}
    units = _build_units(circuits)
    circuit_unit = {member.circuit_id: key for key, unit in units.items() for member in unit.members}

    fuel_pools: dict[str, list[list]] = {}
    for batch in fuel_batches:
        fuel_pools.setdefault(batch.fuel_type, []).append(
            [batch.batch_id, batch.remaining_liters, batch.available_from]
        )
    for pool in fuel_pools.values():
        pool.sort(key=lambda item: (item[2], item[0]))

    def fuel_available(fuel_type: str, when: datetime) -> float:
        return sum(item[1] for item in fuel_pools.get(fuel_type, []) if item[2] <= when)

    soc = battery.soc_kwh if battery else 0.0
    min_run_lock: dict[str, int] = {}
    for key, unit in units.items():
        remaining = 0
        for member in unit.members:
            if member.state != "on":
                continue
            if member.on_since is not None:
                elapsed = (horizon_start - member.on_since).total_seconds() / (interval_minutes * 60.0)
                remaining = max(remaining, member.min_run_intervals - int(elapsed))
            else:
                remaining = max(remaining, member.min_run_intervals)
        if remaining > 0:
            min_run_lock[key] = remaining

    decisions: list[Decision] = []
    reports: list[IntervalReport] = []
    alarms: list[str] = []
    fuel_exhausted_announced: set[str] = set()
    previous_on: set[str] = {
        key for key, unit in units.items() if any(member.state == "on" for member in unit.members)
    }

    for index in range(intervals):
        start = horizon_start + index * delta

        online_generators = [
            gen for gen in generators if gen.online and fuel_available(gen.fuel_type, start) > EPSILON
        ]
        for gen in generators:
            if gen.online and fuel_available(gen.fuel_type, start) <= EPSILON:
                if gen.fuel_type not in fuel_exhausted_announced:
                    fuel_exhausted_announced.add(gen.fuel_type)
                    alarms.append(f"燃料 {gen.fuel_type} 预计于 {start.isoformat()} 前耗尽，对应发电机将停运")
        gen_possible_kwh = 0.0
        for gen in online_generators:
            fuel_cap = fuel_available(gen.fuel_type, start) / gen.liters_per_kwh
            gen_possible_kwh += min(gen.rated_kw * dt_hours, fuel_cap)
        discharge_possible = 0.0
        if battery:
            discharge_possible = min(battery.max_discharge_kw * dt_hours, soc)
        available_kwh = gen_possible_kwh + discharge_possible

        if index in locked:
            on_units = set()
            for circuit_id, (on, _code, _detail) in locked[index].items():
                if on and circuit_id in circuit_unit:
                    on_units.add(circuit_unit[circuit_id])
            unit_reasons: dict[str, tuple[str, str]] = {}
            for circuit_id, (on, code, detail) in locked[index].items():
                if circuit_id in circuit_unit:
                    unit_reasons[circuit_unit[circuit_id]] = (code, f"已执行指令保留：{detail}")
        else:
            on_units, unit_reasons = _dispatch_units(
                units=units,
                start=start,
                windows=windows,
                overrides=overrides,
                min_run_lock=min_run_lock,
                index=index,
                available_kwh=available_kwh,
                dt_hours=dt_hours,
            )

        demand_kwh = 0.0
        for key in on_units:
            demand_kwh += units[key].demand_kw(start, windows) * dt_hours
            if key not in previous_on:
                demand_kwh += units[key].startup_kwh
                if units[key].min_run > 0:
                    min_run_lock[key] = index + units[key].min_run

        discharge_kwh = min(max(demand_kwh - gen_possible_kwh, 0.0), discharge_possible)
        soc -= discharge_kwh
        generation_kwh = min(demand_kwh - discharge_kwh, gen_possible_kwh)
        deficit_kwh = demand_kwh - discharge_kwh - generation_kwh
        if deficit_kwh > EPSILON:
            alarms.append(
                f"区间 {index}（{start.isoformat()}）供应缺口 {deficit_kwh:.3f}kWh，保障负荷供电不足"
            )

        charge_kwh = 0.0
        if battery and battery.max_charge_kw > 0:
            spare = gen_possible_kwh - generation_kwh
            room = (battery.capacity_kwh - soc) / battery.efficiency
            charge_kwh = min(battery.max_charge_kw * dt_hours, max(room, 0.0), max(spare, 0.0))
            soc += charge_kwh * battery.efficiency
        generation_kwh += charge_kwh

        fuel_liters = _allocate_fuel(online_generators, fuel_pools, start, generation_kwh, dt_hours)

        for key, unit in units.items():
            on = key in on_units
            if key in unit_reasons:
                code, detail = unit_reasons[key]
            elif on:
                code, detail = "supply_available", "供应充足，保持供电"
            else:
                code, detail = "deficit_shed", "供应缺口，按保障等级切除"
            for member in unit.members:
                decisions.append(Decision(index, start, member.circuit_id, on, code, detail))

        reports.append(
            IntervalReport(
                index=index,
                start=start,
                demand_kwh=round(demand_kwh, 6),
                generation_kwh=round(generation_kwh, 6),
                charge_kwh=round(charge_kwh, 6),
                discharge_kwh=round(discharge_kwh, 6),
                soc_kwh=round(soc, 6),
                fuel_liters=round(fuel_liters, 6),
                deficit_kwh=round(deficit_kwh, 6),
            )
        )
        previous_on = on_units

    return PlanResult(tuple(decisions), tuple(reports), tuple(alarms))


def _dispatch_units(
    *,
    units: dict[str, _Unit],
    start: datetime,
    windows: list[WindowSpec],
    overrides: list[OverrideSpec],
    min_run_lock: dict[str, int],
    index: int,
    available_kwh: float,
    dt_hours: float,
) -> tuple[set[str], dict[str, tuple[str, str]]]:
    """决定单个区间内各调度单位的供电状态与依据。"""

    reasons: dict[str, tuple[str, str]] = {}
    fixed_on: set[str] = set()
    forced_off: set[str] = set()

    for key, unit in units.items():
        if unit.level in PROTECTED_REASONS:
            fixed_on.add(key)
            reasons[key] = (PROTECTED_REASONS[unit.level], PROTECTED_DETAILS[unit.level])

    for override in overrides:
        if not (override.valid_from <= start < override.valid_until):
            continue
        key = None
        for unit_key, unit in units.items():
            if any(member.circuit_id == override.circuit_id for member in unit.members):
                key = unit_key
                break
        if key is None or units[key].level in PROTECTED_REASONS:
            continue
        if override.action == "force_off":
            forced_off.add(key)
            reasons[key] = ("override_force_off", f"人工覆盖 {override.override_id}（双人确认）要求切除")
        else:
            fixed_on.add(key)
            reasons[key] = ("override_force_on", f"人工覆盖 {override.override_id}（双人确认）要求保持")

    for window in windows:
        if window.start <= start < window.end:
            for key, unit in units.items():
                if key in forced_off:
                    continue
                if any(member.circuit_id == window.circuit_id for member in unit.members):
                    fixed_on.add(key)
                    reasons[key] = ("experiment_window", f"获批实验窗口 {window.window_id} 内必须供电")

    for key, until in min_run_lock.items():
        if index < until and key not in forced_off:
            fixed_on.add(key)
            remaining = until - index
            reasons[key] = ("min_run", f"最低运行时长未满足，还需保持 {remaining} 个区间")

    for key in list(fixed_on):
        for parent in _ancestors(units, key):
            if parent not in fixed_on:
                fixed_on.add(parent)
                forced_off.discard(parent)
                reasons[parent] = ("dependency_required", f"下游回路 {key} 需要供电，上级回路必须保持")

    on_units = {key for key in units if key not in forced_off}

    def demand_of(keys: set[str]) -> float:
        return sum(units[key].demand_kw(start, windows) for key in keys) * dt_hours

    shed_reasons: dict[str, tuple[str, str]] = {}
    while demand_of(on_units) > available_kwh + EPSILON:
        candidates = [key for key in on_units if key not in fixed_on]
        if not candidates:
            break
        victim = max(candidates, key=lambda key: (units[key].level, units[key].demand_kw(start, windows)))
        cascade = {victim} | _descendants(units, victim)
        for key in cascade:
            on_units.discard(key)
            unit = units[key]
            if key == victim:
                if unit.key != unit.members[0].circuit_id or len(unit.members) > 1:
                    detail = (
                        f"供应缺口，成组回路 {unit.key} 整体切除"
                        f"（保障等级 {unit.level}，成员 {len(unit.members)} 个）"
                    )
                else:
                    detail = f"供应缺口，按保障等级 {unit.level} 切除"
                shed_reasons[key] = ("deficit_shed", detail)
            else:
                shed_reasons[key] = ("dependency_parent_off", f"上级回路 {victim} 已切除，随动切除")

    for key, unit in units.items():
        if key in on_units:
            continue
        if key in shed_reasons or key in reasons:
            continue
        off_parents = [parent for parent in unit.parents if parent not in on_units]
        if off_parents:
            reasons[key] = ("dependency_parent_off", f"上级回路 {sorted(off_parents)[0]} 未供电，不能投入")
        else:
            reasons[key] = ("deficit_shed", f"供应缺口，按保障等级 {unit.level} 切除")
    reasons.update(shed_reasons)

    for key in list(on_units):
        for parent in units[key].parents:
            if parent not in on_units:
                on_units.discard(key)
                reasons[key] = ("dependency_parent_off", f"上级回路 {parent} 未供电，不能投入")
                break

    return on_units, reasons


def _allocate_fuel(
    generators: list[GeneratorSpec],
    fuel_pools: dict[str, list[list]],
    start: datetime,
    needed_kwh: float,
    dt_hours: float,
) -> float:
    """把发电量按耗油率从优到劣分摊到发电机，并预扣批次燃料。"""

    remaining_need = needed_kwh
    total_liters = 0.0
    for gen in sorted(generators, key=lambda item: item.liters_per_kwh):
        if remaining_need <= EPSILON:
            break
        pool = fuel_pools.get(gen.fuel_type, [])
        available = sum(item[1] for item in pool if item[2] <= start)
        if available <= EPSILON:
            continue
        cap_kwh = min(gen.rated_kw * dt_hours, available / gen.liters_per_kwh, remaining_need)
        liters = cap_kwh * gen.liters_per_kwh
        for item in pool:
            if item[2] > start or item[1] <= EPSILON:
                continue
            take = min(item[1], liters)
            item[1] -= take
            liters -= take
            if liters <= EPSILON:
                break
        total_liters += cap_kwh * gen.liters_per_kwh
        remaining_need -= cap_kwh
    return total_liters
