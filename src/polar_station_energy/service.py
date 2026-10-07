"""站内能源承诺与负荷处置服务。

在基础服务的角色权限、请求幂等、SQLite 事务与哈希链审计边界上实现：

- 发电机、储能、风机与燃料批次登记，燃料批次带可用时段，补给迟到可登记顺延；
- 配电回路及其保障等级、依赖、成组、最低运行时长、启动代价与分时需求；
- 获批实验窗口与逐时段风电预测；
- 能源承诺方案：草稿 → 双人确认封存 → 被更新方案取代，同一时刻全站只有
  一个封存方案，确认请求重放或并发也只封存一个；
- 人工覆盖：必须两名不同操作者确认且在期限内才生效，保障回路禁止覆盖切除；
- 遥测幂等入账：相同时段重放不重复结算能量，结算按时段单调推进；
- 限电周期：缺口出现即开启，重算只追加动作，恢复供电后由无切除的封存方案关闭；
- 重新规划只计算未来时段，已执行指令保持原样；
- 燃料与能量账目闭合核验，供工程师对账。
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Any

from polar_station_foundation.audit import append_event
from polar_station_foundation.clock import Clock
from polar_station_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from polar_station_foundation.service import DomainService
from polar_station_foundation.storage import Database

from .planner import (
    MUST_TIERS,
    SHED_REASONS,
    SLOT_HOURS,
    SLOT_MINUTES,
    TIERS,
    BatterySpec,
    CircuitSpec,
    FuelBatchSpec,
    GeneratorSpec,
    PlanInput,
    run_plan,
)
from .schema import ensure_energy_schema

FAILURE_TABLES = {
    "generator": ("energy_generators", "generator_id"),
    "battery": ("energy_batteries", "battery_id"),
    "circuit": ("energy_circuits", "circuit_id"),
}


class EnergyService(DomainService):
    """协调能源域的权限、幂等、事务、排程与结算规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        super().__init__(database, clock)
        ensure_energy_schema(database.connection)

    # ------------------------------------------------------------------
    # 基础校验
    # ------------------------------------------------------------------

    def _current_slot(self) -> int:
        return int(self.clock.now().timestamp() // (SLOT_MINUTES * 60))

    def _num(self, value: Any, field: str, *, strict: bool = False, allow_negative: bool = False) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{field} 必须是数字")
        result = float(value)
        if not math.isfinite(result):
            raise ValidationError(f"{field} 必须是有限数字")
        if strict and result <= 0:
            raise ValidationError(f"{field} 必须大于 0")
        if not allow_negative and result < 0:
            raise ValidationError(f"{field} 不能为负数")
        return result

    def _int(self, value: Any, field: str, minimum: int = 0, maximum: int | None = None) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是整数")
        if value < minimum or (maximum is not None and value > maximum):
            raise ValidationError(f"{field} 超出允许范围")
        return value

    def _site_for_actor(self, connection, actor, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        if actor.organization_id != row["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的场所")
        return row

    def _plan_row(self, connection, plan_id: str):
        row = connection.execute(
            "SELECT rowid AS plan_seq, * FROM energy_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("方案不存在")
        return row

    def _circuit_row(self, connection, site_id: str, circuit_id: str):
        row = connection.execute(
            "SELECT * FROM energy_circuits WHERE site_id=? AND circuit_id=?", (site_id, circuit_id)
        ).fetchone()
        if row is None:
            raise NotFoundError("回路不存在")
        return row

    # ------------------------------------------------------------------
    # 资产与回路登记
    # ------------------------------------------------------------------

    def register_generator(self, *, request_id: str, actor_id: str, site_id: str,
                           generator_id: str, rated_kw: float, min_kw: float = 0.0,
                           liters_per_kwh: float = 0.25):
        rated = self._num(rated_kw, "rated_kw", strict=True)
        minimum = self._num(min_kw, "min_kw")
        rate = self._num(liters_per_kwh, "liters_per_kwh", strict=True)
        if minimum > rated:
            raise ValidationError("min_kw 不能大于 rated_kw")
        payload = {"actor_id": actor_id, "site_id": site_id, "generator_id": generator_id,
                   "rated_kw": rated, "min_kw": minimum, "liters_per_kwh": rate}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_actor(connection, actor, site_id)
            generator_id = self._identifier(generator_id, "generator_id")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO energy_generators(generator_id,site_id,rated_kw,min_kw,liters_per_kwh,"
                        "failed_from_slot,created_by,created_at) VALUES(?,?,?,?,?,NULL,?,?)",
                        (generator_id, site_id, rated, minimum, rate, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("发电机编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="energy.generator.registered",
                             resource_type="generator", resource_id=generator_id,
                             detail={"site_id": site_id, "rated_kw": rated}, occurred_at=self._now())
                return "generator", generator_id, {"generator_id": generator_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="energy_register_generator", payload=payload, create=create)

    def register_battery(self, *, request_id: str, actor_id: str, site_id: str,
                         battery_id: str, capacity_kwh: float, max_charge_kw: float,
                         max_discharge_kw: float, efficiency: float, initial_soc_kwh: float):
        capacity = self._num(capacity_kwh, "capacity_kwh", strict=True)
        charge = self._num(max_charge_kw, "max_charge_kw")
        discharge = self._num(max_discharge_kw, "max_discharge_kw")
        eff = self._num(efficiency, "efficiency", strict=True)
        if eff > 1:
            raise ValidationError("efficiency 不能超过 1")
        soc = self._num(initial_soc_kwh, "initial_soc_kwh")
        if soc > capacity:
            raise ValidationError("initial_soc_kwh 不能超过容量")
        payload = {"actor_id": actor_id, "site_id": site_id, "battery_id": battery_id,
                   "capacity_kwh": capacity, "max_charge_kw": charge, "max_discharge_kw": discharge,
                   "efficiency": eff, "initial_soc_kwh": soc}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_actor(connection, actor, site_id)
            battery_id = self._identifier(battery_id, "battery_id")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO energy_batteries(battery_id,site_id,capacity_kwh,max_charge_kw,"
                        "max_discharge_kw,efficiency,initial_soc_kwh,failed_from_slot,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,NULL,?,?)",
                        (battery_id, site_id, capacity, charge, discharge, eff, soc, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("储能编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="energy.battery.registered",
                             resource_type="battery", resource_id=battery_id,
                             detail={"site_id": site_id, "capacity_kwh": capacity}, occurred_at=self._now())
                return "battery", battery_id, {"battery_id": battery_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="energy_register_battery", payload=payload, create=create)

    def register_wind_turbine(self, *, request_id: str, actor_id: str, site_id: str,
                              wind_id: str, rated_kw: float):
        rated = self._num(rated_kw, "rated_kw", strict=True)
        payload = {"actor_id": actor_id, "site_id": site_id, "wind_id": wind_id, "rated_kw": rated}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_actor(connection, actor, site_id)
            wind_id = self._identifier(wind_id, "wind_id")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO energy_wind_turbines(wind_id,site_id,rated_kw,created_by,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (wind_id, site_id, rated, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("风机编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="energy.wind.registered",
                             resource_type="wind_turbine", resource_id=wind_id,
                             detail={"site_id": site_id, "rated_kw": rated}, occurred_at=self._now())
                return "wind_turbine", wind_id, {"wind_id": wind_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="energy_register_wind", payload=payload, create=create)

    def register_fuel_batch(self, *, request_id: str, actor_id: str, site_id: str,
                            batch_id: str, liters: float, available_slot: int | None = None):
        amount = self._num(liters, "liters")
        payload = {"actor_id": actor_id, "site_id": site_id, "batch_id": batch_id,
                   "liters": amount, "available_slot": available_slot}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_actor(connection, actor, site_id)
            batch_id = self._identifier(batch_id, "batch_id")
            slot = self._current_slot() if available_slot is None else self._int(available_slot, "available_slot")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO energy_fuel_batches(batch_id,site_id,liters,remaining_liters,"
                        "available_slot,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (batch_id, site_id, amount, amount, slot, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("燃料批次编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="energy.fuel.registered",
                             resource_type="fuel_batch", resource_id=batch_id,
                             detail={"site_id": site_id, "liters": amount, "available_slot": slot},
                             occurred_at=self._now())
                return "fuel_batch", batch_id, {"batch_id": batch_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="energy_register_fuel_batch", payload=payload, create=create)

    def delay_fuel_batch(self, *, request_id: str, actor_id: str, site_id: str,
                         batch_id: str, available_slot: int):
        """登记燃料补给迟到：批次顺延到更晚的可用时段。"""

        slot = self._int(available_slot, "available_slot")
        payload = {"actor_id": actor_id, "site_id": site_id, "batch_id": batch_id, "available_slot": slot}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_actor(connection, actor, site_id)
            if slot < self._current_slot():
                raise ValidationError("available_slot 不能早于当前时段")

            def create():
                row = connection.execute(
                    "SELECT * FROM energy_fuel_batches WHERE site_id=? AND batch_id=?",
                    (site_id, batch_id)).fetchone()
                if row is None:
                    raise NotFoundError("燃料批次不存在")
                if abs(row["remaining_liters"] - row["liters"]) > 1e-9:
                    raise ConflictError("批次已开始消耗，不能调整可用时段")
                connection.execute(
                    "UPDATE energy_fuel_batches SET available_slot=? WHERE batch_id=?",
                    (slot, batch_id))
                append_event(connection, actor_id=actor_id, action="energy.fuel.delayed",
                             resource_type="fuel_batch", resource_id=batch_id,
                             detail={"site_id": site_id, "from_slot": row["available_slot"], "to_slot": slot},
                             occurred_at=self._now())
                return "fuel_batch", batch_id, {"batch_id": batch_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="energy_delay_fuel_batch", payload=payload, create=create)

    def register_circuit(self, *, request_id: str, actor_id: str, site_id: str,
                         circuit_id: str, name: str, tier: str, default_kw: float,
                         group_id: str | None = None, depends_on: list[str] | None = None,
                         min_run_slots: int = 0, startup_kwh: float = 0.0,
                         shed_rank: int = 0, demand_overrides: dict[Any, Any] | None = None):
        if tier not in TIERS:
            raise ValidationError("tier 不在允许范围内")
        name = self._text(name, "name")
        demand = self._num(default_kw, "default_kw")
        startup = self._num(startup_kwh, "startup_kwh")
        run_slots = self._int(min_run_slots, "min_run_slots", 0, 192)
        rank = self._int(shed_rank, "shed_rank", -1000, 1000)
        depends = list(dict.fromkeys(depends_on or []))
        overrides: dict[str, float] = {}
        for key, value in (demand_overrides or {}).items():
            try:
                slot_key = int(key)
            except (TypeError, ValueError) as exc:
                raise ValidationError("demand_overrides 的时段键必须是整数") from exc
            if slot_key < 0:
                raise ValidationError("demand_overrides 的时段键不能为负")
            overrides[str(slot_key)] = self._num(value, "demand_overrides 的功率")
        if tier in MUST_TIERS and group_id:
            raise ValidationError("保障回路不允许成组")
        if group_id is not None:
            group_id = self._text(group_id, "group_id", 64)
        payload = {"actor_id": actor_id, "site_id": site_id, "circuit_id": circuit_id, "name": name,
                   "tier": tier, "default_kw": demand, "group_id": group_id, "depends_on": depends,
                   "min_run_slots": run_slots, "startup_kwh": startup, "shed_rank": rank,
                   "demand_overrides": overrides}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_actor(connection, actor, site_id)
            circuit_id = self._identifier(circuit_id, "circuit_id")
            for dep in depends:
                self._identifier(dep, "depends_on")
                if dep == circuit_id:
                    raise ValidationError("回路不能依赖自身")
                self._circuit_row(connection, site_id, dep)
            self._check_dependency_cycle(connection, site_id, circuit_id, depends)

            def create():
                try:
                    connection.execute(
                        "INSERT INTO energy_circuits(circuit_id,site_id,name,tier,default_kw,"
                        "demand_overrides_json,group_id,depends_on_json,min_run_slots,startup_kwh,"
                        "shed_rank,failed_from_slot,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
                        (circuit_id, site_id, name, tier, demand, json.dumps(overrides, sort_keys=True),
                         group_id, json.dumps(depends), run_slots, startup, rank, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("回路编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="energy.circuit.registered",
                             resource_type="circuit", resource_id=circuit_id,
                             detail={"site_id": site_id, "tier": tier, "default_kw": demand,
                                     "group_id": group_id, "depends_on": depends},
                             occurred_at=self._now())
                return "circuit", circuit_id, {"circuit_id": circuit_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="energy_register_circuit", payload=payload, create=create)

    def _check_dependency_cycle(self, connection, site_id: str, circuit_id: str, depends: list[str]) -> None:
        graph: dict[str, list[str]] = {circuit_id: list(depends)}
        for row in connection.execute(
                "SELECT circuit_id, depends_on_json FROM energy_circuits WHERE site_id=?", (site_id,)):
            if row["circuit_id"] != circuit_id:
                graph[row["circuit_id"]] = json.loads(row["depends_on_json"])
        seen: set[str] = set()
        stack = [circuit_id]
        while stack:
            current = stack.pop()
            for dep in graph.get(current, []):
                if dep == circuit_id:
                    raise ValidationError("回路依赖存在环")
                if dep not in seen:
                    seen.add(dep)
                    stack.append(dep)

    def approve_experiment_window(self, *, request_id: str, actor_id: str, site_id: str,
                                  window_id: str, circuit_id: str, start_slot: int, end_slot: int):
        start = self._int(start_slot, "start_slot")
        end = self._int(end_slot, "end_slot")
        if end <= start:
            raise ValidationError("end_slot 必须大于 start_slot")
        payload = {"actor_id": actor_id, "site_id": site_id, "window_id": window_id,
                   "circuit_id": circuit_id, "start_slot": start, "end_slot": end}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_actor(connection, actor, site_id)
            window_id = self._identifier(window_id, "window_id")
            circuit = self._circuit_row(connection, site_id, circuit_id)
            if circuit["tier"] != "experiment":
                raise ValidationError("只有实验回路可以登记获批窗口")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO energy_experiment_windows(window_id,site_id,circuit_id,start_slot,"
                        "end_slot,approved_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (window_id, site_id, circuit_id, start, end, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("窗口编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="energy.window.approved",
                             resource_type="experiment_window", resource_id=window_id,
                             detail={"site_id": site_id, "circuit_id": circuit_id,
                                     "start_slot": start, "end_slot": end}, occurred_at=self._now())
                return "experiment_window", window_id, {"window_id": window_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="energy_approve_window", payload=payload, create=create)

    def set_forecast(self, *, request_id: str, actor_id: str, site_id: str,
                     slot: int, wind_kw: float):
        slot = self._int(slot, "slot")
        wind = self._num(wind_kw, "wind_kw")
        payload = {"actor_id": actor_id, "site_id": site_id, "slot": slot, "wind_kw": wind}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_actor(connection, actor, site_id)
            if slot < self._current_slot():
                raise ValidationError("不能修改过去时段的预测")

            def create():
                connection.execute(
                    "INSERT INTO energy_forecasts(site_id,slot,wind_kw,updated_by,updated_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(site_id,slot) DO UPDATE SET wind_kw=excluded.wind_kw,"
                    "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                    (site_id, slot, wind, actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="energy.forecast.updated",
                             resource_type="forecast", resource_id=f"{site_id}:{slot}",
                             detail={"site_id": site_id, "slot": slot, "wind_kw": wind},
                             occurred_at=self._now())
                return "forecast", f"{site_id}:{slot}", {"slot": slot}

            return self._idempotent(connection, request_id=request_id,
                                    action="energy_set_forecast", payload=payload, create=create)

    def report_failure(self, *, request_id: str, actor_id: str, site_id: str,
                       kind: str, asset_id: str, from_slot: int | None = None):
        if kind not in FAILURE_TABLES:
            raise ValidationError("kind 必须是 generator、battery 或 circuit")
        payload = {"actor_id": actor_id, "site_id": site_id, "kind": kind,
                   "asset_id": asset_id, "from_slot": from_slot}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_actor(connection, actor, site_id)
            slot = self._current_slot() if from_slot is None else self._int(from_slot, "from_slot")
            if slot < self._current_slot():
                raise ValidationError("from_slot 不能早于当前时段")
            table, column = FAILURE_TABLES[kind]

            def create():
                row = connection.execute(
                    f"SELECT * FROM {table} WHERE site_id=? AND {column}=?", (site_id, asset_id)).fetchone()
                if row is None:
                    raise NotFoundError("设备不存在")
                if row["failed_from_slot"] is not None:
                    raise ConflictError("设备已处于故障状态")
                connection.execute(
                    f"UPDATE {table} SET failed_from_slot=? WHERE {column}=?", (slot, asset_id))
                append_event(connection, actor_id=actor_id, action="energy.failure.reported",
                             resource_type=kind, resource_id=asset_id,
                             detail={"site_id": site_id, "from_slot": slot}, occurred_at=self._now())
                return kind, asset_id, {"asset_id": asset_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="energy_report_failure", payload=payload, create=create)

    def clear_failure(self, *, request_id: str, actor_id: str, site_id: str,
                      kind: str, asset_id: str):
        if kind not in FAILURE_TABLES:
            raise ValidationError("kind 必须是 generator、battery 或 circuit")
        payload = {"actor_id": actor_id, "site_id": site_id, "kind": kind, "asset_id": asset_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_actor(connection, actor, site_id)
            table, column = FAILURE_TABLES[kind]

            def create():
                row = connection.execute(
                    f"SELECT * FROM {table} WHERE site_id=? AND {column}=?", (site_id, asset_id)).fetchone()
                if row is None:
                    raise NotFoundError("设备不存在")
                if row["failed_from_slot"] is None:
                    raise ConflictError("设备不在故障状态")
                connection.execute(
                    f"UPDATE {table} SET failed_from_slot=NULL WHERE {column}=?", (asset_id,))
                append_event(connection, actor_id=actor_id, action="energy.failure.cleared",
                             resource_type=kind, resource_id=asset_id,
                             detail={"site_id": site_id}, occurred_at=self._now())
                return kind, asset_id, {"asset_id": asset_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="energy_clear_failure", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 能源承诺方案
    # ------------------------------------------------------------------

    def create_plan(self, *, request_id: str, actor_id: str, site_id: str, horizon_slots: int = 48):
        horizon = self._int(horizon_slots, "horizon_slots", 1, 192)
        payload = {"actor_id": actor_id, "site_id": site_id, "horizon_slots": horizon}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_actor(connection, actor, site_id)
            self._expire_overrides(connection, site_id)

            def create():
                plan_id, output, start_slot = self._build_plan(connection, site_id, actor_id, horizon)
                return "plan", plan_id, {"plan_id": plan_id, "base_slot": start_slot,
                                         "breached": output.breached}

            return self._idempotent(connection, request_id=request_id,
                                    action="energy_create_plan", payload=payload, create=create)

    def confirm_plan(self, *, request_id: str, actor_id: str, plan_id: str):
        """双人确认封存：两名不同操作者确认后方案生效，同一时刻只封存一个方案。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            plan = self._plan_row(connection, plan_id)

            def create():
                if plan["state"] == "sealed":
                    raise ConflictError("方案已封存")
                if plan["state"] == "superseded":
                    raise ConflictError("方案已被取代")
                try:
                    connection.execute(
                        "INSERT INTO energy_plan_confirmations(plan_id,actor_id,request_id,created_at) "
                        "VALUES(?,?,?,?)",
                        (plan_id, actor_id, request_id, self._now()))
                except Exception as exc:
                    raise ConflictError("同一操作者不能重复确认同一方案") from exc
                count = connection.execute(
                    "SELECT COUNT(DISTINCT actor_id) AS c FROM energy_plan_confirmations WHERE plan_id=?",
                    (plan_id,)).fetchone()["c"]
                sealed = count >= 2
                if sealed:
                    newer = connection.execute(
                        "SELECT 1 FROM energy_plans WHERE site_id=? AND rowid>? "
                        "AND state IN ('draft','sealed') LIMIT 1",
                        (plan["site_id"], plan["plan_seq"])).fetchone()
                    if newer is not None:
                        raise ConflictError("存在更新的方案，本方案已失效")
                    self._seal_plan(connection, plan, actor_id)
                return "plan", plan_id, {"plan_id": plan_id, "sealed": sealed}

            return self._idempotent(connection, request_id=request_id,
                                    action="energy_confirm_plan", payload=payload, create=create)

    def _build_plan(self, connection, site_id: str, actor_id: str, horizon: int):
        start_slot = self._current_slot()
        inp = self._load_input(connection, site_id, start_slot, horizon)
        output = run_plan(inp)
        plan_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO energy_plans(plan_id,site_id,base_slot,horizon_slots,state,breached,"
            "episode_id,created_by,created_at,sealed_at) VALUES(?,?,?,?,?,?,NULL,?,?,NULL)",
            (plan_id, site_id, start_slot, horizon, "draft", int(output.breached),
             actor_id, self._now()))
        connection.executemany(
            "INSERT INTO energy_plan_directives(plan_id,circuit_id,slot,state,reason_code,reason_detail) "
            "VALUES(?,?,?,?,?,?)",
            [(plan_id, d.circuit_id, d.slot, "on" if d.on else "off", d.reason, d.detail)
             for d in output.directives])
        connection.executemany(
            "INSERT INTO energy_plan_dispatch(plan_id,slot,demand_kw,supply_kw,gen_kw,wind_kw,"
            "battery_kw,soc_kwh,fuel_liters,unserved_kw,breach) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [(plan_id, d.slot, d.demand_kw, d.supply_kw, d.gen_kw, d.wind_kw, d.battery_kw,
              d.soc_kwh, d.fuel_liters, d.unserved_kw, int(d.breach)) for d in output.dispatch])
        append_event(connection, actor_id=actor_id, action="energy.plan.created",
                     resource_type="plan", resource_id=plan_id,
                     detail={"site_id": site_id, "base_slot": start_slot, "horizon_slots": horizon,
                             "breached": output.breached, "shed_count": len(output.sheds)},
                     occurred_at=self._now())
        return plan_id, output, start_slot

    def _seal_plan(self, connection, plan, actor_id: str) -> None:
        site_id = plan["site_id"]
        plan_id = plan["plan_id"]
        previous = connection.execute(
            "SELECT rowid AS plan_seq, * FROM energy_plans WHERE site_id=? AND state='sealed'",
            (site_id,)).fetchone()
        if previous is not None:
            connection.execute("UPDATE energy_plans SET state='superseded' WHERE plan_id=?",
                               (previous["plan_id"],))
            append_event(connection, actor_id=actor_id, action="energy.plan.superseded",
                         resource_type="plan", resource_id=previous["plan_id"],
                         detail={"site_id": site_id, "replaced_by": plan_id}, occurred_at=self._now())
        connection.execute("UPDATE energy_plans SET state='sealed', sealed_at=? WHERE plan_id=?",
                           (self._now(), plan_id))

        shed_rows = connection.execute(
            "SELECT circuit_id, slot, reason_code FROM energy_plan_directives "
            "WHERE plan_id=? AND state='off' AND reason_code IN ('shed_deficit','shed_fuel_reserve',"
            "'shed_dependency','shed_group')", (plan_id,)).fetchall()
        breach_rows = connection.execute(
            "SELECT slot FROM energy_plan_dispatch WHERE plan_id=? AND breach=1", (plan_id,)).fetchall()
        episode = connection.execute(
            "SELECT * FROM energy_episodes WHERE site_id=? AND status='open'", (site_id,)).fetchone()
        episode_id: str | None = None
        if shed_rows or breach_rows:
            if episode is None:
                episode_id = uuid.uuid4().hex
                first_slot = min([r["slot"] for r in shed_rows] + [r["slot"] for r in breach_rows])
                cause = "deficit" if shed_rows else "breach"
                connection.execute(
                    "INSERT INTO energy_episodes(episode_id,site_id,started_slot,ended_slot,status,"
                    "cause,created_at,closed_at) VALUES(?,?,?,NULL,'open',?,?,NULL)",
                    (episode_id, site_id, first_slot, cause, self._now()))
                append_event(connection, actor_id=actor_id, action="energy.episode.opened",
                             resource_type="episode", resource_id=episode_id,
                             detail={"site_id": site_id, "cause": cause, "started_slot": first_slot},
                             occurred_at=self._now())
            else:
                episode_id = episode["episode_id"]
            connection.execute("UPDATE energy_plans SET episode_id=? WHERE plan_id=?",
                               (episode_id, plan_id))
            for row in shed_rows:
                connection.execute(
                    "INSERT OR IGNORE INTO energy_episode_actions(episode_id,circuit_id,slot,action,"
                    "reason_code) VALUES(?,?,?,?,?)",
                    (episode_id, row["circuit_id"], row["slot"], "shed", row["reason_code"]))
            if breach_rows:
                must_ids = [r["circuit_id"] for r in connection.execute(
                    "SELECT circuit_id FROM energy_circuits WHERE site_id=? AND tier IN ('medical','comms','antifreeze')",
                    (site_id,)).fetchall()]
                for row in breach_rows:
                    on_rows = connection.execute(
                        "SELECT circuit_id FROM energy_plan_directives WHERE plan_id=? AND slot=? "
                        "AND state='on'", (plan_id, row["slot"])).fetchall()
                    on_set = {r["circuit_id"] for r in on_rows}
                    for cid in must_ids:
                        connection.execute(
                            "INSERT OR IGNORE INTO energy_episode_actions(episode_id,circuit_id,slot,"
                            "action,reason_code) VALUES(?,?,?,?,?)",
                            (episode_id, cid, row["slot"], "breach",
                             "must_unserved" if cid in on_set else "must_failed"))
        elif episode is not None:
            episode_id = episode["episode_id"]
        # 与被取代方案相比由切除恢复为供电的回路，记入推进中的限电周期
        if episode_id is not None and previous is not None:
            prev_off = connection.execute(
                "SELECT circuit_id, slot FROM energy_plan_directives WHERE plan_id=? AND state='off' "
                "AND reason_code IN ('shed_deficit','shed_fuel_reserve','shed_dependency','shed_group') "
                "AND slot>=?",
                (previous["plan_id"], plan["base_slot"])).fetchall()
            if prev_off:
                new_on = {(r["circuit_id"], r["slot"]) for r in connection.execute(
                    "SELECT circuit_id, slot FROM energy_plan_directives WHERE plan_id=? AND state='on'",
                    (plan_id,)).fetchall()}
                for row in prev_off:
                    if (row["circuit_id"], row["slot"]) in new_on:
                        connection.execute(
                            "INSERT OR IGNORE INTO energy_episode_actions(episode_id,circuit_id,slot,"
                            "action,reason_code) VALUES(?,?,?,?,?)",
                            (episode_id, row["circuit_id"], row["slot"], "restore", "supply_restored"))
        # 无切除且无忧守的封存方案标志供电恢复，未结束的限电周期到此关闭
        if not shed_rows and not breach_rows and episode_id is not None:
            connection.execute(
                "UPDATE energy_episodes SET status='closed', ended_slot=?, closed_at=? WHERE episode_id=?",
                (plan["base_slot"], self._now(), episode_id))
            append_event(connection, actor_id=actor_id, action="energy.episode.closed",
                         resource_type="episode", resource_id=episode_id,
                         detail={"site_id": site_id, "ended_slot": plan["base_slot"]},
                         occurred_at=self._now())
        append_event(connection, actor_id=actor_id, action="energy.plan.sealed",
                     resource_type="plan", resource_id=plan_id,
                     detail={"site_id": site_id, "base_slot": plan["base_slot"],
                             "breached": bool(plan["breached"])}, occurred_at=self._now())

    def resume_site(self, *, request_id: str, actor_id: str, site_id: str, horizon_slots: int = 48):
        """恢复运行后继续推进：封存方案已不覆盖当前时段时，生成延续草稿方案。"""

        horizon = self._int(horizon_slots, "horizon_slots", 1, 192)
        payload = {"actor_id": actor_id, "site_id": site_id, "horizon_slots": horizon}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_actor(connection, actor, site_id)
            self._expire_overrides(connection, site_id)
            now_slot = self._current_slot()
            covering = connection.execute(
                "SELECT plan_id FROM energy_plans WHERE site_id=? AND state='sealed' "
                "AND base_slot<=? AND base_slot+horizon_slots>? ORDER BY base_slot DESC LIMIT 1",
                (site_id, now_slot, now_slot)).fetchone()
            episode = connection.execute(
                "SELECT episode_id FROM energy_episodes WHERE site_id=? AND status='open'",
                (site_id,)).fetchone()

            def create():
                if covering is not None:
                    plan_id = covering["plan_id"]
                    created = False
                else:
                    plan_id, _output, _start = self._build_plan(connection, site_id, actor_id, horizon)
                    created = True
                append_event(connection, actor_id=actor_id, action="energy.site.resumed",
                             resource_type="site", resource_id=site_id,
                             detail={"plan_id": plan_id, "created": created,
                                     "open_episode": episode["episode_id"] if episode else None},
                             occurred_at=self._now())
                return "plan", plan_id, {"plan_id": plan_id, "created": created}

            return self._idempotent(connection, request_id=request_id,
                                    action="energy_resume_site", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 人工覆盖
    # ------------------------------------------------------------------

    def create_override(self, *, request_id: str, actor_id: str, site_id: str,
                        circuit_id: str, slot: int, desired_state: str, reason: str,
                        ttl_slots: int):
        slot = self._int(slot, "slot")
        ttl = self._int(ttl_slots, "ttl_slots", 1, 192)
        if desired_state not in ("on", "off"):
            raise ValidationError("desired_state 必须是 on 或 off")
        reason = self._text(reason, "reason")
        payload = {"actor_id": actor_id, "site_id": site_id, "circuit_id": circuit_id,
                   "slot": slot, "desired_state": desired_state, "reason": reason, "ttl_slots": ttl}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_actor(connection, actor, site_id)
            circuit = self._circuit_row(connection, site_id, circuit_id)
            if desired_state == "off" and circuit["tier"] in MUST_TIERS:
                raise ValidationError("保障回路不允许覆盖切除")
            now_slot = self._current_slot()
            if slot < now_slot:
                raise ValidationError("不能覆盖已经过去的时段")
            expires = now_slot + ttl
            if slot > expires:
                raise ValidationError("目标时段超出覆盖期限")

            def create():
                override_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO energy_overrides(override_id,site_id,circuit_id,slot,desired_state,"
                    "reason,expires_slot,state,created_by,created_at,activated_at) "
                    "VALUES(?,?,?,?,?,?,?,'pending',?,?,NULL)",
                    (override_id, site_id, circuit_id, slot, desired_state, reason, expires,
                     actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="energy.override.created",
                             resource_type="override", resource_id=override_id,
                             detail={"site_id": site_id, "circuit_id": circuit_id, "slot": slot,
                                     "desired_state": desired_state, "expires_slot": expires},
                             occurred_at=self._now())
                return "override", override_id, {"override_id": override_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="energy_create_override", payload=payload, create=create)

    def confirm_override(self, *, request_id: str, actor_id: str, override_id: str):
        """第二名操作者确认后覆盖生效；超期或目标时段已过则作废。"""

        payload = {"actor_id": actor_id, "override_id": override_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            row = connection.execute(
                "SELECT * FROM energy_overrides WHERE override_id=?", (override_id,)).fetchone()
            if row is None:
                raise NotFoundError("覆盖请求不存在")

            def create():
                if row["state"] != "pending":
                    raise ConflictError("覆盖请求不在待确认状态")
                now_slot = self._current_slot()
                if now_slot > row["expires_slot"] or row["slot"] < now_slot:
                    connection.execute(
                        "UPDATE energy_overrides SET state='expired' WHERE override_id=?", (override_id,))
                    append_event(connection, actor_id=actor_id, action="energy.override.expired",
                                 resource_type="override", resource_id=override_id,
                                 detail={"site_id": row["site_id"]}, occurred_at=self._now())
                    raise ConflictError("覆盖确认已超期")
                try:
                    connection.execute(
                        "INSERT INTO energy_override_confirmations(override_id,actor_id,request_id,"
                        "created_at) VALUES(?,?,?,?)",
                        (override_id, actor_id, request_id, self._now()))
                except Exception as exc:
                    raise ConflictError("同一操作者不能重复确认同一覆盖") from exc
                count = connection.execute(
                    "SELECT COUNT(DISTINCT actor_id) AS c FROM energy_override_confirmations "
                    "WHERE override_id=?", (override_id,)).fetchone()["c"]
                active = count >= 2
                if active:
                    connection.execute(
                        "UPDATE energy_overrides SET state='active', activated_at=? WHERE override_id=?",
                        (self._now(), override_id))
                    append_event(connection, actor_id=actor_id, action="energy.override.activated",
                                 resource_type="override", resource_id=override_id,
                                 detail={"site_id": row["site_id"], "circuit_id": row["circuit_id"],
                                         "slot": row["slot"], "desired_state": row["desired_state"]},
                                 occurred_at=self._now())
                return "override", override_id, {"override_id": override_id, "active": active}

            return self._idempotent(connection, request_id=request_id,
                                    action="energy_confirm_override", payload=payload, create=create)

    def _expire_overrides(self, connection, site_id: str) -> None:
        now_slot = self._current_slot()
        rows = connection.execute(
            "SELECT override_id FROM energy_overrides WHERE site_id=? AND state='pending' "
            "AND (expires_slot<? OR slot<?)", (site_id, now_slot, now_slot)).fetchall()
        for row in rows:
            connection.execute("UPDATE energy_overrides SET state='expired' WHERE override_id=?",
                               (row["override_id"],))
            append_event(connection, actor_id="system", action="energy.override.expired",
                         resource_type="override", resource_id=row["override_id"],
                         detail={"site_id": site_id}, occurred_at=self._now())

    # ------------------------------------------------------------------
    # 遥测与结算
    # ------------------------------------------------------------------

    def ingest_telemetry(self, *, request_id: str, actor_id: str, site_id: str, slot: int,
                         generation_kwh: float, wind_kwh: float, load_kwh: float,
                         battery_flow_kwh: float):
        """入账一个时段的实测能量并结算燃料与储能；相同时段重放不会重复结算。"""

        slot = self._int(slot, "slot")
        generation = self._num(generation_kwh, "generation_kwh")
        wind = self._num(wind_kwh, "wind_kwh")
        load = self._num(load_kwh, "load_kwh")
        flow = self._num(battery_flow_kwh, "battery_flow_kwh", allow_negative=True)
        payload = {"actor_id": actor_id, "site_id": site_id, "slot": slot,
                   "generation_kwh": generation, "wind_kwh": wind, "load_kwh": load,
                   "battery_flow_kwh": flow}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_actor(connection, actor, site_id)
            if slot > self._current_slot():
                raise ValidationError("不能结算未来时段")

            def create():
                existing = connection.execute(
                    "SELECT * FROM energy_telemetry WHERE site_id=? AND slot=?",
                    (site_id, slot)).fetchone()
                if existing is not None:
                    same = (abs(existing["generation_kwh"] - generation) <= 1e-9
                            and abs(existing["wind_kwh"] - wind) <= 1e-9
                            and abs(existing["load_kwh"] - load) <= 1e-9
                            and abs(existing["battery_flow_kwh"] - flow) <= 1e-9)
                    if not same:
                        raise ConflictError("该时段遥测与已记录内容不一致")
                    settled = connection.execute(
                        "SELECT settlement_id FROM energy_settlements WHERE site_id=? AND slot=?",
                        (site_id, slot)).fetchone()
                    return "settlement", settled["settlement_id"], {
                        "settlement_id": settled["settlement_id"]}
                later = connection.execute(
                    "SELECT 1 FROM energy_settlements WHERE site_id=? AND slot>=? LIMIT 1",
                    (site_id, slot)).fetchone()
                if later is not None:
                    raise ConflictError("存在更晚时段的结算，不能乱序结算")
                connection.execute(
                    "INSERT INTO energy_telemetry(site_id,slot,generation_kwh,wind_kwh,load_kwh,"
                    "battery_flow_kwh,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (site_id, slot, generation, wind, load, flow, actor_id, self._now()))
                settlement_id = self._settle(connection, site_id, slot, generation, wind, load,
                                             flow, actor_id)
                return "settlement", settlement_id, {"settlement_id": settlement_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="energy_ingest_telemetry", payload=payload, create=create)

    def _settle(self, connection, site_id: str, slot: int, generation: float, wind: float,
                load: float, flow: float, actor_id: str) -> str:
        hours = SLOT_HOURS
        gens = connection.execute(
            "SELECT * FROM energy_generators WHERE site_id=? AND (failed_from_slot IS NULL "
            "OR failed_from_slot>?) ORDER BY rowid", (site_id, slot)).fetchall()
        remaining_gen = generation
        liters_total = 0.0
        last_rate = None
        for g in gens:
            alloc = min(remaining_gen, g["rated_kw"] * hours)
            liters_total += alloc * g["liters_per_kwh"]
            last_rate = g["liters_per_kwh"]
            remaining_gen -= alloc
            if remaining_gen <= 1e-9:
                break
        if remaining_gen > 1e-9 and last_rate:
            liters_total += remaining_gen * last_rate
        # 燃料批次按可用时段先后支取
        batches = connection.execute(
            "SELECT * FROM energy_fuel_batches WHERE site_id=? AND available_slot<=? "
            "AND remaining_liters>0 ORDER BY available_slot, batch_id", (site_id, slot)).fetchall()
        left = liters_total
        fuel_short = 0
        for batch in batches:
            take = min(batch["remaining_liters"], left)
            if take > 1e-12:
                connection.execute(
                    "UPDATE energy_fuel_batches SET remaining_liters=remaining_liters-? WHERE batch_id=?",
                    (take, batch["batch_id"]))
                connection.execute(
                    "INSERT INTO energy_fuel_ledger(site_id,slot,batch_id,liters) VALUES(?,?,?,?)",
                    (site_id, slot, batch["batch_id"], take))
                left -= take
            if left <= 1e-9:
                break
        if left > 1e-9:
            fuel_short = 1
        # 储能电量按容量比例分摊充放
        bats = connection.execute(
            "SELECT * FROM energy_batteries WHERE site_id=? ORDER BY rowid", (site_id,)).fetchall()
        total_capacity = sum(b["capacity_kwh"] for b in bats)
        new_socs: dict[str, float] = {}
        for b in bats:
            previous = self._soc_at(connection, site_id, b["battery_id"], slot,
                                    b["initial_soc_kwh"])
            share = b["capacity_kwh"] / total_capacity if total_capacity > 0 else 0.0
            battery_flow = flow * share
            if battery_flow >= 0:
                available = min(b["max_discharge_kw"] * hours, previous * b["efficiency"])
                if battery_flow - available > 0.05:
                    raise ValidationError("遥测放电超出储能能力")
                new_soc = previous - battery_flow / b["efficiency"]
            else:
                charge = -battery_flow
                room = (b["capacity_kwh"] - previous) / b["efficiency"]
                available = min(b["max_charge_kw"] * hours, room)
                if charge - available > 0.05:
                    raise ValidationError("遥测充电超出储能能力")
                new_soc = previous + charge * b["efficiency"]
            new_socs[b["battery_id"]] = min(max(new_soc, 0.0), b["capacity_kwh"])
        for battery_id, value in new_socs.items():
            connection.execute(
                "INSERT INTO energy_battery_soc(site_id,battery_id,slot,soc_kwh) VALUES(?,?,?,?)",
                (site_id, battery_id, slot, value))
        planned = connection.execute(
            "SELECT d.demand_kw FROM energy_plans p JOIN energy_plan_dispatch d "
            "ON d.plan_id=p.plan_id AND d.slot=? WHERE p.site_id=? AND p.state IN ('sealed','superseded') "
            "AND p.base_slot<=? AND p.base_slot+p.horizon_slots>? "
            "ORDER BY p.base_slot DESC, p.rowid DESC LIMIT 1",
            (slot, site_id, slot, slot)).fetchone()
        planned_kwh = planned["demand_kw"] * hours if planned else 0.0
        unserved = max(0.0, planned_kwh - load)
        imbalance = generation + wind + flow - load
        settlement_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO energy_settlements(settlement_id,site_id,slot,generation_kwh,wind_kwh,"
            "load_kwh,battery_flow_kwh,planned_kwh,fuel_liters,unserved_kwh,imbalance_kwh,soc_kwh,"
            "fuel_short,settled_by,settled_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (settlement_id, site_id, slot, generation, wind, load, flow, planned_kwh, liters_total,
             unserved, imbalance, sum(new_socs.values()), fuel_short, actor_id, self._now()))
        append_event(connection, actor_id=actor_id, action="energy.telemetry.settled",
                     resource_type="settlement", resource_id=settlement_id,
                     detail={"site_id": site_id, "slot": slot, "fuel_liters": liters_total,
                             "unserved_kwh": unserved, "imbalance_kwh": imbalance,
                             "fuel_short": fuel_short}, occurred_at=self._now())
        return settlement_id

    def _soc_at(self, connection, site_id: str, battery_id: str, slot: int, default: float) -> float:
        row = connection.execute(
            "SELECT soc_kwh FROM energy_battery_soc WHERE site_id=? AND battery_id=? AND slot<? "
            "ORDER BY slot DESC LIMIT 1", (site_id, battery_id, slot)).fetchone()
        return row["soc_kwh"] if row else default

    # ------------------------------------------------------------------
    # 查询与对账
    # ------------------------------------------------------------------

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        connection = self.database.connection
        plan = connection.execute(
            "SELECT * FROM energy_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None:
            raise NotFoundError("方案不存在")
        dispatch = [dict(row) for row in connection.execute(
            "SELECT * FROM energy_plan_dispatch WHERE plan_id=? ORDER BY slot", (plan_id,)).fetchall()]
        confirmations = [row["actor_id"] for row in connection.execute(
            "SELECT actor_id FROM energy_plan_confirmations WHERE plan_id=? ORDER BY created_at",
            (plan_id,)).fetchall()]
        return {"plan_id": plan["plan_id"], "site_id": plan["site_id"],
                "base_slot": plan["base_slot"], "horizon_slots": plan["horizon_slots"],
                "state": plan["state"], "breached": bool(plan["breached"]),
                "episode_id": plan["episode_id"], "created_by": plan["created_by"],
                "created_at": plan["created_at"], "sealed_at": plan["sealed_at"],
                "confirmations": confirmations, "dispatch": dispatch}

    def plan_directives(self, plan_id: str, slot: int | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        if connection.execute("SELECT 1 FROM energy_plans WHERE plan_id=?",
                              (plan_id,)).fetchone() is None:
            raise NotFoundError("方案不存在")
        query = ("SELECT circuit_id, slot, state, reason_code, reason_detail "
                 "FROM energy_plan_directives WHERE plan_id=?")
        params: list[Any] = [plan_id]
        if slot is not None:
            query += " AND slot=?"
            params.append(slot)
        query += " ORDER BY slot, circuit_id"
        return [dict(row) for row in connection.execute(query, params).fetchall()]

    def list_plans(self, site_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT plan_id, base_slot, horizon_slots, state, breached, episode_id, created_by,"
            "created_at, sealed_at FROM energy_plans WHERE site_id=? ORDER BY base_slot, plan_id",
            (site_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["breached"] = bool(item["breached"])
            result.append(item)
        return result

    def settlements(self, site_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM energy_settlements WHERE site_id=? ORDER BY slot", (site_id,)).fetchall()
        return [dict(row) for row in rows]

    def curtailment_status(self, site_id: str) -> dict[str, Any]:
        connection = self.database.connection
        open_episode = connection.execute(
            "SELECT * FROM energy_episodes WHERE site_id=? AND status='open'", (site_id,)).fetchone()
        latest = connection.execute(
            "SELECT * FROM energy_episodes WHERE site_id=? ORDER BY started_slot DESC, rowid DESC "
            "LIMIT 1", (site_id,)).fetchone()
        shown = open_episode if open_episode is not None else latest
        actions: list[dict[str, Any]] = []
        if shown is not None:
            actions = [dict(row) for row in connection.execute(
                "SELECT circuit_id, slot, action, reason_code FROM energy_episode_actions "
                "WHERE episode_id=? ORDER BY slot, circuit_id",
                (shown["episode_id"],)).fetchall()]
        closed = connection.execute(
            "SELECT COUNT(*) AS c FROM energy_episodes WHERE site_id=? AND status='closed'",
            (site_id,)).fetchone()["c"]
        return {"site_id": site_id,
                "open_episode": dict(open_episode) if open_episode else None,
                "latest_episode": dict(latest) if latest else None,
                "actions": actions,
                "closed_episodes": closed}

    def site_overview(self, site_id: str) -> dict[str, Any]:
        connection = self.database.connection
        now_slot = self._current_slot()
        fuel_rows = connection.execute(
            "SELECT batch_id, liters, remaining_liters, available_slot FROM energy_fuel_batches "
            "WHERE site_id=? ORDER BY available_slot, batch_id", (site_id,)).fetchall()
        batteries = []
        for row in connection.execute(
                "SELECT battery_id, capacity_kwh, initial_soc_kwh, failed_from_slot "
                "FROM energy_batteries WHERE site_id=? ORDER BY rowid", (site_id,)).fetchall():
            batteries.append({
                "battery_id": row["battery_id"], "capacity_kwh": row["capacity_kwh"],
                "soc_kwh": self._soc_at(connection, site_id, row["battery_id"], now_slot,
                                        row["initial_soc_kwh"]),
                "failed": row["failed_from_slot"] is not None
                           and now_slot >= row["failed_from_slot"]})
        plan = connection.execute(
            "SELECT plan_id, base_slot, horizon_slots, breached FROM energy_plans "
            "WHERE site_id=? AND state='sealed' ORDER BY base_slot DESC LIMIT 1", (site_id,)).fetchone()
        circuits = []
        if plan is not None:
            for row in connection.execute(
                    "SELECT c.circuit_id, c.tier, d.state, d.reason_code, d.reason_detail "
                    "FROM energy_circuits c LEFT JOIN energy_plan_directives d "
                    "ON d.plan_id=? AND d.circuit_id=c.circuit_id AND d.slot=? "
                    "WHERE c.site_id=? ORDER BY c.circuit_id",
                    (plan["plan_id"], now_slot, site_id)).fetchall():
                circuits.append(dict(row))
        episode = connection.execute(
            "SELECT episode_id, started_slot, cause FROM energy_episodes "
            "WHERE site_id=? AND status='open'", (site_id,)).fetchone()
        return {
            "site_id": site_id,
            "current_slot": now_slot,
            "fuel": {
                "remaining_liters": sum(r["remaining_liters"] for r in fuel_rows),
                "available_liters": sum(r["remaining_liters"] for r in fuel_rows
                                        if r["available_slot"] <= now_slot),
                "batches": [dict(r) for r in fuel_rows],
            },
            "batteries": batteries,
            "active_plan": dict(plan) if plan else None,
            "open_episode": dict(episode) if episode else None,
            "circuits": circuits,
        }

    def reconcile(self, site_id: str, tolerance_kwh: float = 0.05) -> dict[str, Any]:
        """核验燃料与能量账目闭合：收入 = 消耗 + 剩余，供给 = 需求 ± 储能。"""

        connection = self.database.connection
        fuel = connection.execute(
            "SELECT COALESCE(SUM(liters),0) AS total, COALESCE(SUM(remaining_liters),0) AS remaining "
            "FROM energy_fuel_batches WHERE site_id=?", (site_id,)).fetchone()
        consumed = connection.execute(
            "SELECT COALESCE(SUM(liters),0) AS c FROM energy_fuel_ledger WHERE site_id=?",
            (site_id,)).fetchone()["c"]
        fuel_gap = fuel["total"] - fuel["remaining"] - consumed
        fuel_closed = abs(fuel_gap) <= 1e-6
        rows = connection.execute(
            "SELECT slot, generation_kwh, wind_kwh, load_kwh, battery_flow_kwh, planned_kwh,"
            "imbalance_kwh, unserved_kwh, fuel_short FROM energy_settlements "
            "WHERE site_id=? ORDER BY slot", (site_id,)).fetchall()
        violations = [{"slot": r["slot"], "imbalance_kwh": r["imbalance_kwh"]}
                      for r in rows if abs(r["imbalance_kwh"]) > tolerance_kwh]
        energy_closed = not violations
        total_imbalance = sum(r["imbalance_kwh"] for r in rows)
        planned_total = sum(r["planned_kwh"] for r in rows)
        load_total = sum(r["load_kwh"] for r in rows)
        unserved_total = sum(r["unserved_kwh"] for r in rows)
        return {
            "site_id": site_id,
            "fuel": {
                "received_liters": fuel["total"],
                "consumed_liters": consumed,
                "remaining_liters": fuel["remaining"],
                "gap_liters": fuel_gap,
                "closed": fuel_closed,
            },
            "energy": {
                "settlements": len(rows),
                "total_imbalance_kwh": total_imbalance,
                "tolerance_kwh": tolerance_kwh,
                "violations": violations,
                "closed": energy_closed,
            },
            "planned_vs_actual": {
                "planned_kwh": planned_total,
                "load_kwh": load_total,
                "unserved_kwh": unserved_total,
                "fuel_short_slots": sum(1 for r in rows if r["fuel_short"]),
            },
            "closed": fuel_closed and energy_closed,
        }

    # ------------------------------------------------------------------
    # 排程输入装配
    # ------------------------------------------------------------------

    def _load_input(self, connection, site_id: str, start_slot: int, horizon: int) -> PlanInput:
        end_slot = start_slot + horizon
        circuits = tuple(
            CircuitSpec(
                circuit_id=row["circuit_id"], tier=row["tier"], default_kw=row["default_kw"],
                demand_overrides={int(k): float(v) for k, v in
                                  json.loads(row["demand_overrides_json"]).items()},
                group_id=row["group_id"],
                depends_on=tuple(json.loads(row["depends_on_json"])),
                min_run_slots=row["min_run_slots"], startup_kwh=row["startup_kwh"],
                shed_rank=row["shed_rank"], failed_from_slot=row["failed_from_slot"])
            for row in connection.execute(
                "SELECT * FROM energy_circuits WHERE site_id=? ORDER BY rowid", (site_id,)).fetchall())
        generators = tuple(
            GeneratorSpec(row["generator_id"], row["rated_kw"], row["min_kw"],
                          row["liters_per_kwh"], row["failed_from_slot"])
            for row in connection.execute(
                "SELECT * FROM energy_generators WHERE site_id=? ORDER BY rowid", (site_id,)).fetchall())
        batteries = tuple(
            BatterySpec(row["battery_id"], row["capacity_kwh"], row["max_charge_kw"],
                        row["max_discharge_kw"], row["efficiency"],
                        self._soc_at(connection, site_id, row["battery_id"], start_slot,
                                     row["initial_soc_kwh"]),
                        row["failed_from_slot"])
            for row in connection.execute(
                "SELECT * FROM energy_batteries WHERE site_id=? ORDER BY rowid", (site_id,)).fetchall())
        fuel_batches = tuple(
            FuelBatchSpec(row["batch_id"], row["remaining_liters"], row["available_slot"])
            for row in connection.execute(
                "SELECT * FROM energy_fuel_batches WHERE site_id=? AND remaining_liters>0 "
                "ORDER BY available_slot, batch_id", (site_id,)).fetchall())
        wind = {row["slot"]: row["wind_kw"] for row in connection.execute(
            "SELECT slot, wind_kw FROM energy_forecasts WHERE site_id=? AND slot>=? AND slot<?",
            (site_id, start_slot, end_slot)).fetchall()}
        windows: dict[str, list[tuple[int, int]]] = {}
        for row in connection.execute(
                "SELECT circuit_id, start_slot, end_slot FROM energy_experiment_windows "
                "WHERE site_id=?", (site_id,)).fetchall():
            windows.setdefault(row["circuit_id"], []).append((row["start_slot"], row["end_slot"]))
        overrides = {
            (row["circuit_id"], row["slot"]): row["desired_state"]
            for row in connection.execute(
                "SELECT circuit_id, slot, desired_state FROM energy_overrides "
                "WHERE site_id=? AND state='active' AND slot>=? AND slot<? AND expires_slot>=slot",
                (site_id, start_slot, end_slot)).fetchall()}
        circuit_rows = connection.execute(
            "SELECT circuit_id, min_run_slots FROM energy_circuits WHERE site_id=?",
            (site_id,)).fetchall()
        prior_on, on_since = self._prior_states(connection, site_id, start_slot, circuit_rows)
        return PlanInput(
            start_slot=start_slot, horizon=horizon, slot_hours=SLOT_HOURS,
            circuits=circuits, generators=generators, batteries=batteries,
            fuel_batches=fuel_batches, wind_kw=wind,
            windows={k: tuple(v) for k, v in windows.items()}, overrides=overrides,
            prior_on=prior_on, on_since=on_since)

    def _prior_states(self, connection, site_id: str, start_slot: int,
                      circuit_rows) -> tuple[dict[str, bool], dict[str, int]]:
        """从已封存/被取代的方案回读上一时段投切状态与最低运行时长起点。"""

        plans = connection.execute(
            "SELECT plan_id, base_slot, horizon_slots FROM energy_plans "
            "WHERE site_id=? AND state IN ('sealed','superseded') "
            "ORDER BY base_slot DESC, rowid DESC",
            (site_id,)).fetchall()
        target = start_slot - 1
        covering = next(
            (p for p in plans if p["base_slot"] <= target < p["base_slot"] + p["horizon_slots"]), None)
        prior_on: dict[str, bool] = {}
        on_since: dict[str, int] = {}
        if covering is None:
            return prior_on, on_since
        for row in connection.execute(
                "SELECT circuit_id, state FROM energy_plan_directives WHERE plan_id=? AND slot=?",
                (covering["plan_id"], target)).fetchall():
            prior_on[row["circuit_id"]] = row["state"] == "on"
        max_run = max((r["min_run_slots"] for r in circuit_rows), default=0)
        if not max_run:
            return prior_on, on_since
        need_map = {r["circuit_id"]: r["min_run_slots"] for r in circuit_rows}
        for cid, is_on in prior_on.items():
            need = need_map.get(cid, 0)
            if not is_on or not need:
                continue
            since = target
            slot = target - 1
            while slot >= target - max_run + 1:
                owner = next(
                    (p for p in plans if p["base_slot"] <= slot < p["base_slot"] + p["horizon_slots"]),
                    None)
                if owner is None:
                    break
                row = connection.execute(
                    "SELECT state FROM energy_plan_directives WHERE plan_id=? AND circuit_id=? AND slot=?",
                    (owner["plan_id"], cid, slot)).fetchone()
                if row is None or row["state"] != "on":
                    break
                since = slot
                slot -= 1
            if target - since + 1 < need:
                on_since[cid] = since
        return prior_on, on_since
