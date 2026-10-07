"""站内能源承诺与负荷处置服务。

在基础服务的权限、幂等、事务与审计边界上，把发电、储能、燃料批次和
分时需求连成逐区间预测；出现缺口时先守住医疗、通信和防冻约束；
实际遥测、设备故障或燃料补给迟到后只重算未来区段，已执行指令保持原样。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .clock import Clock
from .energy_planner import (
    BatterySpec,
    CircuitSpec,
    FuelBatchSpec,
    GeneratorSpec,
    OverrideSpec,
    WindowSpec,
    compute_plan,
)
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .service import DomainService
from .storage import Database


ENERGY_ROLES = ("admin", "operator")
SETTLEMENT_TOLERANCE_KWH = 0.5


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def _floor(value: datetime, interval_minutes: int) -> datetime:
    step = interval_minutes * 60
    return datetime.fromtimestamp(int(value.timestamp()) // step * step, tz=timezone.utc)


class EnergyService(DomainService):
    """能源承诺与负荷处置的领域服务。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        super().__init__(database, clock)

    # ------------------------------------------------------------------
    # 通用校验与装载
    # ------------------------------------------------------------------

    def _site_for(self, connection, actor, site_id: str):
        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if site is None:
            raise NotFoundError("场所不存在")
        if actor.organization_id != site["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的场所")
        return site

    def _energy_actor(self, connection, actor_id: str):
        actor = self._actor(connection, actor_id)
        self._require(actor, *ENERGY_ROLES)
        return actor

    def _circuit_row(self, connection, site_id: str, circuit_id: str):
        row = connection.execute(
            "SELECT * FROM energy_circuits WHERE circuit_id=? AND site_id=?",
            (circuit_id, site_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("配电回路不存在")
        return row

    def _active_plan(self, connection, site_id: str, now: datetime):
        return connection.execute(
            "SELECT * FROM energy_plans WHERE site_id=? AND status='sealed' "
            "AND horizon_start<=? AND horizon_end>? ORDER BY horizon_start DESC LIMIT 1",
            (site_id, _iso(now), _iso(now)),
        ).fetchone()

    def _expire_overrides(self, connection, site_id: str, now: datetime) -> None:
        connection.execute(
            "UPDATE energy_overrides SET status='expired' "
            "WHERE site_id=? AND status IN ('pending','confirmed') AND valid_until<=?",
            (site_id, _iso(now)),
        )

    def _planning_inputs(self, connection, site_id: str,
                         horizon_start: datetime, horizon_end: datetime):
        generators = [
            GeneratorSpec(
                generator_id=row["generator_id"],
                rated_kw=row["rated_kw"],
                fuel_type=row["fuel_type"],
                liters_per_kwh=row["liters_per_kwh"],
                online=row["status"] == "online",
            )
            for row in connection.execute(
                "SELECT * FROM energy_generators WHERE site_id=?", (site_id,)
            )
        ]
        batches = [
            FuelBatchSpec(
                batch_id=row["batch_id"],
                fuel_type=row["fuel_type"],
                remaining_liters=row["remaining_liters"],
                available_from=_parse_ts(row["available_from"], "available_from"),
            )
            for row in connection.execute(
                "SELECT * FROM energy_fuel_batches WHERE site_id=? AND remaining_liters>0",
                (site_id,),
            )
        ]
        battery_rows = connection.execute(
            "SELECT * FROM energy_batteries WHERE site_id=?", (site_id,)
        ).fetchall()
        battery = None
        if battery_rows:
            battery = BatterySpec(
                capacity_kwh=sum(row["capacity_kwh"] for row in battery_rows),
                max_charge_kw=sum(row["max_charge_kw"] for row in battery_rows),
                max_discharge_kw=sum(row["max_discharge_kw"] for row in battery_rows),
                soc_kwh=sum(row["soc_kwh"] for row in battery_rows),
                efficiency=min(row["efficiency"] for row in battery_rows),
            )
        circuits = [
            CircuitSpec(
                circuit_id=row["circuit_id"],
                name=row["name"],
                protection_level=row["protection_level"],
                demand_kw=row["demand_kw"],
                profile=tuple(json.loads(row["profile_json"])),
                depends_on=row["depends_on"],
                group_id=row["group_id"],
                min_run_intervals=row["min_run_intervals"],
                startup_kwh=row["startup_kwh"],
                state=row["state"],
                on_since=_parse_ts(row["on_since"], "on_since") if row["on_since"] else None,
            )
            for row in connection.execute(
                "SELECT * FROM energy_circuits WHERE site_id=?", (site_id,)
            )
        ]
        windows = [
            WindowSpec(
                window_id=row["window_id"],
                circuit_id=row["circuit_id"],
                start=_parse_ts(row["start_at"], "start_at"),
                end=_parse_ts(row["end_at"], "end_at"),
                required_kw=row["required_kw"],
            )
            for row in connection.execute(
                "SELECT * FROM energy_experiment_windows WHERE site_id=? "
                "AND status='approved' AND end_at>? AND start_at<?",
                (site_id, _iso(horizon_start), _iso(horizon_end)),
            )
        ]
        overrides = [
            OverrideSpec(
                override_id=row["override_id"],
                circuit_id=row["circuit_id"],
                action=row["action"],
                valid_from=_parse_ts(row["valid_from"], "valid_from"),
                valid_until=_parse_ts(row["valid_until"], "valid_until"),
            )
            for row in connection.execute(
                "SELECT * FROM energy_overrides WHERE site_id=? "
                "AND status='confirmed' AND valid_until>? AND valid_from<?",
                (site_id, _iso(horizon_start), _iso(horizon_end)),
            )
        ]
        return generators, batches, battery, circuits, windows, overrides

    # ------------------------------------------------------------------
    # 资产与约束登记
    # ------------------------------------------------------------------

    def register_generator(self, *, request_id: str, actor_id: str, site_id: str,
                           generator_id: str, name: str, rated_kw: float,
                           fuel_type: str, liters_per_kwh: float) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "generator_id": generator_id,
                   "name": name, "rated_kw": rated_kw, "fuel_type": fuel_type,
                   "liters_per_kwh": liters_per_kwh}
        with self.database.transaction(immediate=True) as connection:
            actor = self._energy_actor(connection, actor_id)
            self._site_for(connection, actor, site_id)
            generator_id = self._identifier(generator_id, "generator_id")
            name = self._text(name, "name")
            fuel_type = self._identifier(fuel_type, "fuel_type")
            rated_kw = self._positive(rated_kw, "rated_kw")
            liters_per_kwh = self._positive(liters_per_kwh, "liters_per_kwh")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO energy_generators(generator_id,site_id,name,rated_kw,fuel_type,"
                        "liters_per_kwh,status,created_at) VALUES(?,?,?,?,?,?,'online',?)",
                        (generator_id, site_id, name, rated_kw, fuel_type,
                         liters_per_kwh, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("发电机编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="energy_generator.registered",
                             resource_type="energy_generator", resource_id=generator_id,
                             detail={"site_id": site_id, "rated_kw": rated_kw,
                                     "fuel_type": fuel_type}, occurred_at=self._now())
                return "energy_generator", generator_id, {"generator_id": generator_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_generator", payload=payload, create=create)

    def register_fuel_batch(self, *, request_id: str, actor_id: str, site_id: str,
                            batch_id: str, fuel_type: str, quantity_liters: float,
                            available_from: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "batch_id": batch_id,
                   "fuel_type": fuel_type, "quantity_liters": quantity_liters,
                   "available_from": available_from}
        with self.database.transaction(immediate=True) as connection:
            actor = self._energy_actor(connection, actor_id)
            self._site_for(connection, actor, site_id)
            batch_id = self._identifier(batch_id, "batch_id")
            fuel_type = self._identifier(fuel_type, "fuel_type")
            quantity = self._non_negative(quantity_liters, "quantity_liters")
            available = _parse_ts(available_from, "available_from")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO energy_fuel_batches(batch_id,site_id,fuel_type,quantity_liters,"
                        "remaining_liters,available_from,created_at) VALUES(?,?,?,?,?,?,?)",
                        (batch_id, site_id, fuel_type, quantity, quantity,
                         _iso(available), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("燃料批次编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="fuel_batch.registered",
                             resource_type="fuel_batch", resource_id=batch_id,
                             detail={"site_id": site_id, "fuel_type": fuel_type,
                                     "quantity_liters": quantity,
                                     "available_from": _iso(available)},
                             occurred_at=self._now())
                return "fuel_batch", batch_id, {"batch_id": batch_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_fuel_batch", payload=payload, create=create)

    def register_battery(self, *, request_id: str, actor_id: str, site_id: str,
                         battery_id: str, capacity_kwh: float, max_charge_kw: float,
                         max_discharge_kw: float, soc_kwh: float,
                         efficiency: float = 0.95) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "battery_id": battery_id,
                   "capacity_kwh": capacity_kwh, "max_charge_kw": max_charge_kw,
                   "max_discharge_kw": max_discharge_kw, "soc_kwh": soc_kwh,
                   "efficiency": efficiency}
        with self.database.transaction(immediate=True) as connection:
            actor = self._energy_actor(connection, actor_id)
            self._site_for(connection, actor, site_id)
            battery_id = self._identifier(battery_id, "battery_id")
            capacity = self._positive(capacity_kwh, "capacity_kwh")
            charge = self._non_negative(max_charge_kw, "max_charge_kw")
            discharge = self._non_negative(max_discharge_kw, "max_discharge_kw")
            soc = self._non_negative(soc_kwh, "soc_kwh")
            if soc > capacity:
                raise ValidationError("soc_kwh 不能超过容量")
            efficiency = float(efficiency)
            if not 0 < efficiency <= 1:
                raise ValidationError("efficiency 必须在 (0, 1] 内")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO energy_batteries(battery_id,site_id,capacity_kwh,max_charge_kw,"
                        "max_discharge_kw,soc_kwh,efficiency,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (battery_id, site_id, capacity, charge, discharge, soc,
                         efficiency, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("储能编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="battery.registered",
                             resource_type="battery", resource_id=battery_id,
                             detail={"site_id": site_id, "capacity_kwh": capacity},
                             occurred_at=self._now())
                return "battery", battery_id, {"battery_id": battery_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_battery", payload=payload, create=create)

    def register_circuit(self, *, request_id: str, actor_id: str, site_id: str,
                         circuit_id: str, name: str, protection_level: int,
                         demand_kw: float, profile: list[dict[str, Any]] | None = None,
                         depends_on: str | None = None, group_id: str | None = None,
                         min_run_intervals: int = 0, startup_kwh: float = 0.0,
                         state: str = "off") -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "circuit_id": circuit_id,
                   "name": name, "protection_level": protection_level, "demand_kw": demand_kw,
                   "profile": profile, "depends_on": depends_on, "group_id": group_id,
                   "min_run_intervals": min_run_intervals, "startup_kwh": startup_kwh,
                   "state": state}
        with self.database.transaction(immediate=True) as connection:
            actor = self._energy_actor(connection, actor_id)
            self._site_for(connection, actor, site_id)
            circuit_id = self._identifier(circuit_id, "circuit_id")
            name = self._text(name, "name")
            level = int(protection_level)
            if not 1 <= level <= 9:
                raise ValidationError("protection_level 必须在 1 到 9 之间")
            demand = self._non_negative(demand_kw, "demand_kw")
            profile = self._profile(profile)
            if depends_on is not None:
                depends_on = self._identifier(depends_on, "depends_on")
                self._circuit_row(connection, site_id, depends_on)
                self._check_dependency_cycle(connection, site_id, circuit_id, depends_on)
            if group_id is not None:
                group_id = self._identifier(group_id, "group_id")
            min_run = int(min_run_intervals)
            if min_run < 0:
                raise ValidationError("min_run_intervals 不能为负")
            startup = self._non_negative(startup_kwh, "startup_kwh")
            if state not in ("on", "off"):
                raise ValidationError("state 必须是 on 或 off")
            on_since = self._now() if state == "on" else None

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO energy_circuits(circuit_id,site_id,name,protection_level,"
                        "demand_kw,profile_json,depends_on,group_id,min_run_intervals,"
                        "startup_kwh,state,on_since,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (circuit_id, site_id, name, level, demand, canonical_json(profile),
                         depends_on, group_id, min_run, startup, state, on_since, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("配电回路编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="circuit.registered",
                             resource_type="circuit", resource_id=circuit_id,
                             detail={"site_id": site_id, "protection_level": level,
                                     "group_id": group_id, "depends_on": depends_on},
                             occurred_at=self._now())
                return "circuit", circuit_id, {"circuit_id": circuit_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_circuit", payload=payload, create=create)

    def approve_experiment_window(self, *, request_id: str, actor_id: str, site_id: str,
                                  window_id: str, circuit_id: str, start: str, end: str,
                                  required_kw: float) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "window_id": window_id,
                   "circuit_id": circuit_id, "start": start, "end": end,
                   "required_kw": required_kw}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            self._site_for(connection, actor, site_id)
            window_id = self._identifier(window_id, "window_id")
            self._circuit_row(connection, site_id, circuit_id)
            start_at = _parse_ts(start, "start")
            end_at = _parse_ts(end, "end")
            if end_at <= start_at:
                raise ValidationError("实验窗口结束必须晚于开始")
            required = self._non_negative(required_kw, "required_kw")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO energy_experiment_windows(window_id,site_id,circuit_id,"
                        "start_at,end_at,required_kw,approved_by,status,created_at) "
                        "VALUES(?,?,?,?,?,?,?,'approved',?)",
                        (window_id, site_id, circuit_id, _iso(start_at), _iso(end_at),
                         required, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("实验窗口编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="experiment_window.approved",
                             resource_type="experiment_window", resource_id=window_id,
                             detail={"site_id": site_id, "circuit_id": circuit_id,
                                     "start": _iso(start_at), "end": _iso(end_at)},
                             occurred_at=self._now())
                return "experiment_window", window_id, {"window_id": window_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="approve_experiment_window", payload=payload,
                                    create=create)

    # ------------------------------------------------------------------
    # 人工覆盖（双人确认 + 期限）
    # ------------------------------------------------------------------

    def request_override(self, *, request_id: str, actor_id: str, site_id: str,
                         override_id: str, circuit_id: str, action: str,
                         valid_from: str, valid_until: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "override_id": override_id,
                   "circuit_id": circuit_id, "action": action,
                   "valid_from": valid_from, "valid_until": valid_until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._energy_actor(connection, actor_id)
            self._site_for(connection, actor, site_id)
            override_id = self._identifier(override_id, "override_id")
            circuit = self._circuit_row(connection, site_id, circuit_id)
            if action not in ("force_on", "force_off"):
                raise ValidationError("action 必须是 force_on 或 force_off")
            if action == "force_off" and circuit["protection_level"] <= 3:
                raise ValidationError("医疗、通信和防冻回路不允许人工切除")
            start = _parse_ts(valid_from, "valid_from")
            until = _parse_ts(valid_until, "valid_until")
            if until <= start:
                raise ValidationError("覆盖期限结束必须晚于开始")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO energy_overrides(override_id,site_id,circuit_id,action,"
                        "valid_from,valid_until,status,requested_by,created_at) "
                        "VALUES(?,?,?,?,?,?,'pending',?,?)",
                        (override_id, site_id, circuit_id, action, _iso(start),
                         _iso(until), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("覆盖编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="override.requested",
                             resource_type="override", resource_id=override_id,
                             detail={"site_id": site_id, "circuit_id": circuit_id,
                                     "action": action, "valid_until": _iso(until)},
                             occurred_at=self._now())
                return "override", override_id, {"override_id": override_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="request_override", payload=payload, create=create)

    def confirm_override(self, *, request_id: str, actor_id: str,
                         override_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "override_id": override_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._energy_actor(connection, actor_id)
            row = connection.execute(
                "SELECT * FROM energy_overrides WHERE override_id=?", (override_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("人工覆盖不存在")
            self._site_for(connection, actor, row["site_id"])
            now = self.clock.now()
            if row["status"] == "expired" or _parse_ts(row["valid_until"], "valid_until") <= now:
                connection.execute(
                    "UPDATE energy_overrides SET status='expired' WHERE override_id=?",
                    (override_id,),
                )
                raise ValidationError("覆盖期限已过，不能确认")
            if row["status"] != "pending":
                raise ConflictError("人工覆盖已确认或已失效")
            if row["requested_by"] == actor_id:
                raise PermissionDenied("人工覆盖必须由第二名操作者确认")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE energy_overrides SET status='confirmed', confirmed_by=?, "
                    "confirmed_at=? WHERE override_id=?",
                    (actor_id, self._now(), override_id),
                )
                append_event(connection, actor_id=actor_id, action="override.confirmed",
                             resource_type="override", resource_id=override_id,
                             detail={"site_id": row["site_id"], "circuit_id": row["circuit_id"],
                                     "action": row["action"], "requested_by": row["requested_by"],
                                     "confirmed_by": actor_id},
                             occurred_at=self._now())
                return "override", override_id, {"override_id": override_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="confirm_override", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 方案计算、封存与重算
    # ------------------------------------------------------------------

    def _compute_plan_in_tx(self, connection, *, actor_id: str, site_id: str,
                            trigger: str, based_on: str | None,
                            horizon_intervals: int, interval_minutes: int,
                            locked: dict[int, dict[str, tuple[bool, str, str]]] | None,
                            sealed: bool, note: str | None = None) -> str:
        now = self.clock.now()
        self._expire_overrides(connection, site_id, now)
        horizon_start = _floor(now, interval_minutes)
        horizon_end = horizon_start + timedelta(minutes=interval_minutes * horizon_intervals)
        generators, batches, battery, circuits, windows, overrides = self._planning_inputs(
            connection, site_id, horizon_start, horizon_end
        )
        if not circuits:
            raise ValidationError("场所尚未登记配电回路，无法制定能源方案")
        result = compute_plan(
            horizon_start=horizon_start,
            interval_minutes=interval_minutes,
            intervals=horizon_intervals,
            generators=generators,
            battery=battery,
            fuel_batches=batches,
            circuits=circuits,
            windows=windows,
            overrides=overrides,
            locked=locked,
        )
        plan_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO energy_plans(plan_id,site_id,horizon_start,horizon_end,"
            "interval_minutes,status,trigger,based_on,alarms_json,created_at,sealed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, site_id, _iso(horizon_start), _iso(horizon_end), interval_minutes,
             "sealed" if sealed else "draft", trigger, based_on,
             canonical_json(list(result.alarms)), self._now(),
             self._now() if sealed else None),
        )
        for decision in result.decisions:
            connection.execute(
                "INSERT INTO energy_plan_decisions(plan_id,interval_index,interval_start,"
                "circuit_id,decision,reason_code,reason_detail) VALUES(?,?,?,?,?,?,?)",
                (plan_id, decision.interval_index, _iso(decision.interval_start),
                 decision.circuit_id, "on" if decision.on else "off",
                 decision.reason_code, decision.reason_detail),
            )
        for report in result.intervals:
            connection.execute(
                "INSERT INTO energy_plan_intervals(plan_id,interval_index,interval_start,"
                "demand_kwh,generation_kwh,charge_kwh,discharge_kwh,soc_kwh,fuel_liters,"
                "deficit_kwh) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (plan_id, report.index, _iso(report.start), report.demand_kwh,
                 report.generation_kwh, report.charge_kwh, report.discharge_kwh,
                 report.soc_kwh, report.fuel_liters, report.deficit_kwh),
            )
        action = "plan.replanned" if based_on else "plan.computed"
        append_event(connection, actor_id=actor_id, action=action,
                     resource_type="energy_plan", resource_id=plan_id,
                     detail={"site_id": site_id, "trigger": trigger, "based_on": based_on,
                             "horizon_start": _iso(horizon_start),
                             "horizon_end": _iso(horizon_end), "sealed": sealed,
                             "alarms": list(result.alarms), "note": note},
                     occurred_at=self._now())
        return plan_id

    def compute_plan(self, *, request_id: str, actor_id: str, site_id: str,
                     horizon_intervals: int = 24, interval_minutes: int = 60) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id,
                   "horizon_intervals": horizon_intervals, "interval_minutes": interval_minutes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._energy_actor(connection, actor_id)
            self._site_for(connection, actor, site_id)
            intervals = int(horizon_intervals)
            minutes = int(interval_minutes)
            if not 1 <= intervals <= 24 * 14:
                raise ValidationError("horizon_intervals 超出允许范围")
            if minutes < 5 or 24 * 60 % minutes != 0:
                raise ValidationError("interval_minutes 必须不小于 5 且能整除一天")

            def create() -> tuple[str, str, dict[str, Any]]:
                plan_id = self._compute_plan_in_tx(
                    connection, actor_id=actor_id, site_id=site_id, trigger="manual",
                    based_on=None, horizon_intervals=intervals, interval_minutes=minutes,
                    locked=None, sealed=False,
                )
                return "energy_plan", plan_id, {"plan_id": plan_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="compute_plan", payload=payload, create=create)

    def seal_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._energy_actor(connection, actor_id)
            plan = connection.execute(
                "SELECT * FROM energy_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise NotFoundError("能源方案不存在")
            self._site_for(connection, actor, plan["site_id"])
            if plan["status"] == "sealed":
                def sealed_receipt() -> tuple[str, str, dict[str, Any]]:
                    return "energy_plan", plan_id, {"plan_id": plan_id}

                return self._idempotent(connection, request_id=request_id,
                                        action="seal_plan", payload=payload,
                                        create=sealed_receipt)
            if plan["status"] != "draft":
                raise ConflictError("方案已被替代，不能封存")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT plan_id FROM energy_plans WHERE site_id=? AND horizon_start=? "
                    "AND status='sealed'",
                    (plan["site_id"], plan["horizon_start"]),
                ).fetchone()
                if existing is not None:
                    raise ConflictError("同一时段已经封存了其他方案")
                connection.execute(
                    "UPDATE energy_plans SET status='sealed', sealed_at=? WHERE plan_id=?",
                    (self._now(), plan_id),
                )
                append_event(connection, actor_id=actor_id, action="plan.sealed",
                             resource_type="energy_plan", resource_id=plan_id,
                             detail={"site_id": plan["site_id"],
                                     "horizon_start": plan["horizon_start"]},
                             occurred_at=self._now())
                return "energy_plan", plan_id, {"plan_id": plan_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="seal_plan", payload=payload, create=create)

    def _replan_in_tx(self, connection, *, actor_id: str, site_id: str,
                      trigger: str, note: str | None) -> str:
        """只重算未来区段：当前区间沿用已执行指令，其余重新计算。"""

        now = self.clock.now()
        active = self._active_plan(connection, site_id, now)
        interval_minutes = active["interval_minutes"] if active else 60
        horizon_start = _floor(now, interval_minutes)
        locked: dict[int, dict[str, tuple[bool, str, str]]] = {}
        based_on = None
        if active is not None:
            horizon_end = _parse_ts(active["horizon_end"], "horizon_end")
            based_on = active["plan_id"]
            rows = connection.execute(
                "SELECT * FROM energy_plan_decisions WHERE plan_id=? AND interval_start=?",
                (active["plan_id"], _iso(horizon_start)),
            ).fetchall()
            if rows:
                locked[0] = {
                    row["circuit_id"]: (row["decision"] == "on",
                                        row["reason_code"], row["reason_detail"])
                    for row in rows
                }
            connection.execute(
                "UPDATE energy_plans SET status='superseded' WHERE plan_id=?",
                (active["plan_id"],),
            )
            intervals = max(1, int((horizon_end - horizon_start).total_seconds()
                                   // (interval_minutes * 60)))
        else:
            intervals = 24
        return self._compute_plan_in_tx(
            connection, actor_id=actor_id, site_id=site_id, trigger=trigger,
            based_on=based_on, horizon_intervals=intervals,
            interval_minutes=interval_minutes, locked=locked, sealed=True, note=note,
        )

    def replan(self, *, request_id: str, actor_id: str, site_id: str, trigger: str,
               note: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "trigger": trigger, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._energy_actor(connection, actor_id)
            self._site_for(connection, actor, site_id)
            trigger = self._identifier(trigger, "trigger")

            def create() -> tuple[str, str, dict[str, Any]]:
                plan_id = self._replan_in_tx(connection, actor_id=actor_id,
                                             site_id=site_id, trigger=trigger, note=note)
                return "energy_plan", plan_id, {"plan_id": plan_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="replan", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 实际事件：遥测结算、设备状态、燃料迟到
    # ------------------------------------------------------------------

    def record_telemetry(self, *, request_id: str, actor_id: str, site_id: str,
                         telemetry_id: str, interval_start: str, interval_end: str,
                         generators: list[dict[str, Any]],
                         battery_soc_kwh: float | None = None,
                         circuits: list[dict[str, Any]] | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "telemetry_id": telemetry_id,
                   "interval_start": interval_start, "interval_end": interval_end,
                   "generators": generators, "battery_soc_kwh": battery_soc_kwh,
                   "circuits": circuits}
        with self.database.transaction(immediate=True) as connection:
            actor = self._energy_actor(connection, actor_id)
            self._site_for(connection, actor, site_id)
            telemetry_id = self._identifier(telemetry_id, "telemetry_id")
            start = _parse_ts(interval_start, "interval_start")
            end = _parse_ts(interval_end, "interval_end")
            if end <= start:
                raise ValidationError("遥测区间结束必须晚于开始")
            if not isinstance(generators, list) or not generators:
                raise ValidationError("generators 必须是非空数组")
            telemetry_hash = digest({"telemetry_id": telemetry_id, "interval_start": _iso(start),
                                     "interval_end": _iso(end), "generators": generators,
                                     "battery_soc_kwh": battery_soc_kwh, "circuits": circuits})

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM energy_telemetry WHERE telemetry_id=?", (telemetry_id,)
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != telemetry_hash:
                        raise ConflictError("同一遥测编号的内容不一致")
                    settled = connection.execute(
                        "SELECT * FROM energy_settlements WHERE telemetry_id=?",
                        (telemetry_id,),
                    ).fetchone()
                    return "energy_settlement", settled["settlement_id"], {
                        "settlement_id": settled["settlement_id"], "telemetry_replayed": True,
                    }

                generated_kwh = 0.0
                fuel_liters = 0.0
                for reading in generators:
                    generator = connection.execute(
                        "SELECT * FROM energy_generators WHERE generator_id=? AND site_id=?",
                        (reading.get("generator_id"), site_id),
                    ).fetchone()
                    if generator is None:
                        raise NotFoundError("遥测引用的发电机不存在")
                    kwh = self._non_negative(reading.get("kwh"), "kwh")
                    generated_kwh += kwh
                    needed = kwh * generator["liters_per_kwh"]
                    fuel_liters += needed
                    batches = connection.execute(
                        "SELECT * FROM energy_fuel_batches WHERE site_id=? AND fuel_type=? "
                        "AND remaining_liters>0 AND available_from<=? "
                        "ORDER BY available_from, batch_id",
                        (site_id, generator["fuel_type"], _iso(start)),
                    ).fetchall()
                    for batch in batches:
                        if needed <= 0:
                            break
                        take = min(batch["remaining_liters"], needed)
                        connection.execute(
                            "UPDATE energy_fuel_batches SET remaining_liters=remaining_liters-? "
                            "WHERE batch_id=?",
                            (take, batch["batch_id"]),
                        )
                        needed -= take

                battery_delta = 0.0
                if battery_soc_kwh is not None:
                    rows = connection.execute(
                        "SELECT * FROM energy_batteries WHERE site_id=?", (site_id,)
                    ).fetchall()
                    if not rows:
                        raise ValidationError("场所没有储能，不能上报储能电量")
                    capacity = sum(row["capacity_kwh"] for row in rows)
                    new_soc = self._non_negative(battery_soc_kwh, "battery_soc_kwh")
                    if new_soc > capacity + 1e-6:
                        raise ValidationError("上报储能电量超过总容量")
                    old_soc = sum(row["soc_kwh"] for row in rows)
                    battery_delta = new_soc - old_soc
                    for row in rows:
                        share = row["capacity_kwh"] / capacity if capacity else 0
                        connection.execute(
                            "UPDATE energy_batteries SET soc_kwh=? WHERE battery_id=?",
                            (round(new_soc * share, 6), row["battery_id"]),
                        )

                delivered_kwh = 0.0
                for entry in circuits or []:
                    circuit = self._circuit_row(connection, site_id, entry.get("circuit_id"))
                    delivered_kwh += self._non_negative(entry.get("kwh"), "kwh")
                    state = entry.get("state")
                    if state == "on" and circuit["state"] != "on":
                        connection.execute(
                            "UPDATE energy_circuits SET state='on', on_since=? WHERE circuit_id=?",
                            (_iso(start), circuit["circuit_id"]),
                        )
                    elif state == "off" and circuit["state"] != "off":
                        connection.execute(
                            "UPDATE energy_circuits SET state='off', on_since=NULL "
                            "WHERE circuit_id=?",
                            (circuit["circuit_id"],),
                        )

                imbalance = generated_kwh - delivered_kwh - battery_delta
                tolerance = max(SETTLEMENT_TOLERANCE_KWH, 0.01 * generated_kwh)
                closed = 1 if abs(imbalance) <= tolerance else 0
                settlement_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO energy_telemetry(telemetry_id,site_id,interval_start,"
                    "interval_end,payload_hash,created_at) VALUES(?,?,?,?,?,?)",
                    (telemetry_id, site_id, _iso(start), _iso(end),
                     telemetry_hash, self._now()),
                )
                connection.execute(
                    "INSERT INTO energy_settlements(settlement_id,telemetry_id,site_id,"
                    "interval_start,generated_kwh,delivered_kwh,battery_delta_kwh,"
                    "fuel_liters,closed,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (settlement_id, telemetry_id, site_id, _iso(start), generated_kwh,
                     delivered_kwh, battery_delta, fuel_liters, closed, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="telemetry.settled",
                             resource_type="energy_settlement", resource_id=settlement_id,
                             detail={"site_id": site_id, "telemetry_id": telemetry_id,
                                     "interval_start": _iso(start),
                                     "generated_kwh": generated_kwh,
                                     "delivered_kwh": delivered_kwh,
                                     "battery_delta_kwh": battery_delta,
                                     "fuel_liters": fuel_liters, "closed": bool(closed)},
                             occurred_at=self._now())
                return "energy_settlement", settlement_id, {
                    "settlement_id": settlement_id, "closed": bool(closed),
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="record_telemetry", payload=payload, create=create)

    def delay_fuel_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                         new_available_from: str, reason: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "batch_id": batch_id,
                   "new_available_from": new_available_from, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._energy_actor(connection, actor_id)
            batch = connection.execute(
                "SELECT * FROM energy_fuel_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise NotFoundError("燃料批次不存在")
            self._site_for(connection, actor, batch["site_id"])
            available = _parse_ts(new_available_from, "new_available_from")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE energy_fuel_batches SET available_from=? WHERE batch_id=?",
                    (_iso(available), batch_id),
                )
                append_event(connection, actor_id=actor_id, action="fuel_batch.delayed",
                             resource_type="fuel_batch", resource_id=batch_id,
                             detail={"site_id": batch["site_id"],
                                     "old_available_from": batch["available_from"],
                                     "new_available_from": _iso(available),
                                     "reason": reason},
                             occurred_at=self._now())
                plan_id = None
                if self._active_plan(connection, batch["site_id"], self.clock.now()):
                    plan_id = self._replan_in_tx(
                        connection, actor_id=actor_id, site_id=batch["site_id"],
                        trigger="fuel_delay",
                        note=f"燃料批次 {batch_id} 迟到：{reason or '未说明'}",
                    )
                return "fuel_batch", batch_id, {"batch_id": batch_id, "replan_id": plan_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="delay_fuel_batch", payload=payload, create=create)

    def report_equipment_status(self, *, request_id: str, actor_id: str, site_id: str,
                                equipment_type: str, equipment_id: str,
                                status: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id,
                   "equipment_type": equipment_type, "equipment_id": equipment_id,
                   "status": status}
        with self.database.transaction(immediate=True) as connection:
            actor = self._energy_actor(connection, actor_id)
            self._site_for(connection, actor, site_id)
            if equipment_type == "generator":
                row = connection.execute(
                    "SELECT * FROM energy_generators WHERE generator_id=? AND site_id=?",
                    (equipment_id, site_id),
                ).fetchone()
                if row is None:
                    raise NotFoundError("发电机不存在")
                if status not in ("online", "offline", "fault"):
                    raise ValidationError("发电机状态必须是 online、offline 或 fault")
            elif equipment_type == "circuit":
                row = self._circuit_row(connection, site_id, equipment_id)
                if status not in ("on", "off"):
                    raise ValidationError("回路状态必须是 on 或 off")
            else:
                raise ValidationError("equipment_type 必须是 generator 或 circuit")

            def create() -> tuple[str, str, dict[str, Any]]:
                if equipment_type == "generator":
                    connection.execute(
                        "UPDATE energy_generators SET status=? WHERE generator_id=?",
                        (status, equipment_id),
                    )
                else:
                    if status == "on":
                        connection.execute(
                            "UPDATE energy_circuits SET state='on', on_since=? "
                            "WHERE circuit_id=?",
                            (self._now(), equipment_id),
                        )
                    else:
                        connection.execute(
                            "UPDATE energy_circuits SET state='off', on_since=NULL "
                            "WHERE circuit_id=?",
                            (equipment_id,),
                        )
                append_event(connection, actor_id=actor_id, action="equipment.status_reported",
                             resource_type=equipment_type, resource_id=equipment_id,
                             detail={"site_id": site_id, "status": status},
                             occurred_at=self._now())
                plan_id = None
                if self._active_plan(connection, site_id, self.clock.now()):
                    plan_id = self._replan_in_tx(
                        connection, actor_id=actor_id, site_id=site_id,
                        trigger="equipment_status",
                        note=f"{equipment_type} {equipment_id} 状态变为 {status}",
                    )
                return equipment_type, equipment_id, {
                    "equipment_id": equipment_id, "status": status, "replan_id": plan_id,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="report_equipment_status", payload=payload,
                                    create=create)

    # ------------------------------------------------------------------
    # 查询：调度状态、处置依据、账目闭合
    # ------------------------------------------------------------------

    def list_plans(self, site_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM energy_plans WHERE site_id=? ORDER BY horizon_start, created_at",
            (site_id,),
        ).fetchall()
        return [self._plan_header(row) for row in rows]

    def get_plan_rationale(self, plan_id: str) -> dict[str, Any]:
        connection = self.database.connection
        plan = connection.execute(
            "SELECT * FROM energy_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if plan is None:
            raise NotFoundError("能源方案不存在")
        names = {
            row["circuit_id"]: row["name"]
            for row in connection.execute(
                "SELECT circuit_id, name FROM energy_circuits WHERE site_id=?",
                (plan["site_id"],),
            )
        }
        circuits: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
            "SELECT * FROM energy_plan_decisions WHERE plan_id=? "
            "ORDER BY circuit_id, interval_index",
            (plan_id,),
        ):
            entry = circuits.setdefault(row["circuit_id"], {
                "circuit_id": row["circuit_id"],
                "name": names.get(row["circuit_id"], row["circuit_id"]),
                "intervals": [],
            })
            entry["intervals"].append({
                "interval_index": row["interval_index"],
                "interval_start": row["interval_start"],
                "decision": row["decision"],
                "reason_code": row["reason_code"],
                "reason_detail": row["reason_detail"],
            })
        resources = [
            {"interval_index": row["interval_index"], "interval_start": row["interval_start"],
             "demand_kwh": row["demand_kwh"], "generation_kwh": row["generation_kwh"],
             "charge_kwh": row["charge_kwh"], "discharge_kwh": row["discharge_kwh"],
             "soc_kwh": row["soc_kwh"], "fuel_liters": row["fuel_liters"],
             "deficit_kwh": row["deficit_kwh"]}
            for row in connection.execute(
                "SELECT * FROM energy_plan_intervals WHERE plan_id=? ORDER BY interval_index",
                (plan_id,),
            )
        ]
        return {"plan": self._plan_header(plan),
                "circuits": list(circuits.values()), "resources": resources}

    def get_dispatch_state(self, site_id: str) -> dict[str, Any]:
        connection = self.database.connection
        now = self.clock.now()
        active = self._active_plan(connection, site_id, now)
        current = None
        alarms: list[str] = []
        if active is not None:
            interval_start = _iso(_floor(now, active["interval_minutes"]))
            names = {
                row["circuit_id"]: row["name"]
                for row in connection.execute(
                    "SELECT circuit_id, name FROM energy_circuits WHERE site_id=?", (site_id,)
                )
            }
            decisions = [
                {"circuit_id": row["circuit_id"], "name": names.get(row["circuit_id"],
                                                                    row["circuit_id"]),
                 "decision": row["decision"], "reason_code": row["reason_code"],
                 "reason_detail": row["reason_detail"]}
                for row in connection.execute(
                    "SELECT * FROM energy_plan_decisions WHERE plan_id=? AND interval_start=? "
                    "ORDER BY circuit_id",
                    (active["plan_id"], interval_start),
                )
            ]
            resource = connection.execute(
                "SELECT * FROM energy_plan_intervals WHERE plan_id=? AND interval_start=?",
                (active["plan_id"], interval_start),
            ).fetchone()
            current = {
                "interval_start": interval_start,
                "decisions": decisions,
                "resources": None if resource is None else {
                    "demand_kwh": resource["demand_kwh"],
                    "generation_kwh": resource["generation_kwh"],
                    "discharge_kwh": resource["discharge_kwh"],
                    "soc_kwh": resource["soc_kwh"],
                    "fuel_liters": resource["fuel_liters"],
                    "deficit_kwh": resource["deficit_kwh"],
                },
            }
            alarms = list(json.loads(active["alarms_json"]))
        return {
            "site_id": site_id,
            "now": _iso(now),
            "active_plan": None if active is None else self._plan_header(active),
            "current_interval": current,
            "curtailments": self._curtailments(connection, site_id, active, now),
            "fuel": self._fuel_status(connection, site_id),
            "battery": self._battery_status(connection, site_id),
            "generators": [
                {"generator_id": row["generator_id"], "status": row["status"],
                 "rated_kw": row["rated_kw"], "fuel_type": row["fuel_type"]}
                for row in connection.execute(
                    "SELECT * FROM energy_generators WHERE site_id=? ORDER BY generator_id",
                    (site_id,),
                )
            ],
            "alarms": alarms,
        }

    def verify_energy_account(self, site_id: str) -> dict[str, Any]:
        connection = self.database.connection
        settlements = connection.execute(
            "SELECT * FROM energy_settlements WHERE site_id=? ORDER BY interval_start",
            (site_id,),
        ).fetchall()
        generated = sum(row["generated_kwh"] for row in settlements)
        delivered = sum(row["delivered_kwh"] for row in settlements)
        battery_delta = sum(row["battery_delta_kwh"] for row in settlements)
        fuel_settled = sum(row["fuel_liters"] for row in settlements)
        fuel_drawn = sum(
            row["quantity_liters"] - row["remaining_liters"]
            for row in connection.execute(
                "SELECT * FROM energy_fuel_batches WHERE site_id=?", (site_id,)
            )
        )
        energy_error = generated - delivered - battery_delta
        fuel_error = fuel_settled - fuel_drawn
        unclosed = [row["interval_start"] for row in settlements if not row["closed"]]
        closed = (not unclosed
                  and abs(energy_error) <= SETTLEMENT_TOLERANCE_KWH
                  and abs(fuel_error) <= SETTLEMENT_TOLERANCE_KWH)
        return {
            "site_id": site_id,
            "settlements": len(settlements),
            "generated_kwh": round(generated, 6),
            "delivered_kwh": round(delivered, 6),
            "battery_delta_kwh": round(battery_delta, 6),
            "fuel_settled_liters": round(fuel_settled, 6),
            "fuel_drawn_liters": round(fuel_drawn, 6),
            "energy_balance_error_kwh": round(energy_error, 6),
            "fuel_balance_error_liters": round(fuel_error, 6),
            "unclosed_intervals": unclosed,
            "closed": closed,
        }

    # ------------------------------------------------------------------
    # 内部查询工具
    # ------------------------------------------------------------------

    def _plan_header(self, row) -> dict[str, Any]:
        return {"plan_id": row["plan_id"], "site_id": row["site_id"],
                "horizon_start": row["horizon_start"], "horizon_end": row["horizon_end"],
                "interval_minutes": row["interval_minutes"], "status": row["status"],
                "trigger": row["trigger"], "based_on": row["based_on"],
                "alarms": list(json.loads(row["alarms_json"])),
                "created_at": row["created_at"], "sealed_at": row["sealed_at"]}

    def _curtailments(self, connection, site_id: str, active, now: datetime) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if active is not None:
            superseded = connection.execute(
                "SELECT * FROM energy_plans WHERE site_id=? AND status='superseded' "
                "AND horizon_start<? ORDER BY horizon_start",
                (site_id, active["horizon_start"]),
            ).fetchall()
            for plan in superseded:
                rows.extend(self._plan_off_rows(connection, plan, active["horizon_start"]))
            rows.extend(self._plan_off_rows(connection, active, None))
        periods: dict[str, list[dict[str, Any]]] = {}
        for row in sorted(rows, key=lambda item: (item["circuit_id"], item["interval_start"])):
            bucket = periods.setdefault(row["circuit_id"], [])
            start = _parse_ts(row["interval_start"], "interval_start")
            end = start + timedelta(minutes=row["interval_minutes"])
            if bucket and bucket[-1]["end"] >= row["interval_start"]:
                bucket[-1]["end"] = max(bucket[-1]["end"], _iso(end))
                bucket[-1]["intervals"] += 1
                if row["reason_code"] not in bucket[-1]["reason_codes"]:
                    bucket[-1]["reason_codes"].append(row["reason_code"])
            else:
                bucket.append({"circuit_id": row["circuit_id"],
                               "start": row["interval_start"], "end": _iso(end),
                               "intervals": 1, "reason_codes": [row["reason_code"]]})
        result = []
        for circuit_id in sorted(periods):
            for period in periods[circuit_id]:
                if period["end"] <= _iso(now):
                    status = "completed"
                elif period["start"] > _iso(now):
                    status = "upcoming"
                else:
                    status = "active"
                result.append({**period, "status": status})
        return result

    def _plan_off_rows(self, connection, plan, before: str | None) -> list[dict[str, Any]]:
        query = ("SELECT circuit_id, interval_start, reason_code FROM energy_plan_decisions "
                 "WHERE plan_id=? AND decision='off'")
        parameters: list[Any] = [plan["plan_id"]]
        if before is not None:
            query += " AND interval_start<?"
            parameters.append(before)
        return [{"circuit_id": row["circuit_id"], "interval_start": row["interval_start"],
                 "reason_code": row["reason_code"],
                 "interval_minutes": plan["interval_minutes"]}
                for row in connection.execute(query, parameters)]

    def _fuel_status(self, connection, site_id: str) -> list[dict[str, Any]]:
        pools: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
            "SELECT * FROM energy_fuel_batches WHERE site_id=? ORDER BY fuel_type, available_from",
            (site_id,),
        ):
            pool = pools.setdefault(row["fuel_type"], {
                "fuel_type": row["fuel_type"], "remaining_liters": 0.0, "batches": 0,
            })
            pool["remaining_liters"] += row["remaining_liters"]
            pool["batches"] += 1
        for pool in pools.values():
            pool["remaining_liters"] = round(pool["remaining_liters"], 6)
        return list(pools.values())

    def _battery_status(self, connection, site_id: str) -> dict[str, Any] | None:
        rows = connection.execute(
            "SELECT * FROM energy_batteries WHERE site_id=?", (site_id,)
        ).fetchall()
        if not rows:
            return None
        return {"soc_kwh": round(sum(row["soc_kwh"] for row in rows), 6),
                "capacity_kwh": round(sum(row["capacity_kwh"] for row in rows), 6)}

    # ------------------------------------------------------------------
    # 数值与资料校验
    # ------------------------------------------------------------------

    def _positive(self, value: Any, field: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是数值") from exc
        if number <= 0:
            raise ValidationError(f"{field} 必须大于 0")
        return number

    def _non_negative(self, value: Any, field: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是数值") from exc
        if number < 0:
            raise ValidationError(f"{field} 不能为负")
        return number

    def _profile(self, profile: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        if profile is None:
            return []
        if not isinstance(profile, list):
            raise ValidationError("profile 必须是数组")
        cleaned = []
        for entry in profile:
            try:
                start = float(entry["start_hour"])
                end = float(entry["end_hour"])
                kw = float(entry["kw"])
            except (TypeError, ValueError, KeyError) as exc:
                raise ValidationError("profile 条目必须包含 start_hour、end_hour 和 kw") from exc
            if not (0 <= start < 24) or not (0 < end <= 24) or kw < 0:
                raise ValidationError("profile 条目的小时或功率无效")
            cleaned.append({"start_hour": start, "end_hour": end, "kw": kw})
        return cleaned

    def _check_dependency_cycle(self, connection, site_id: str,
                                circuit_id: str, depends_on: str) -> None:
        seen = {circuit_id}
        current = depends_on
        while current is not None:
            if current in seen:
                raise ValidationError("回路依赖不能成环")
            seen.add(current)
            row = connection.execute(
                "SELECT depends_on FROM energy_circuits WHERE circuit_id=? AND site_id=?",
                (current, site_id),
            ).fetchone()
            current = row["depends_on"] if row else None
