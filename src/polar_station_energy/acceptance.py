"""站内能源承诺与负荷处置系统的离线端到端验收。

场景：极夜大风导致风机停运，全站依靠一台柴油发电机与有限燃料运行。
验收覆盖：保障等级守护、成组投切、依赖联动、最低运行时长、启动代价、
获批实验窗口、燃料批次分时可用与配给、双人确认封存、人工覆盖、遥测幂等
结算、故障与补给迟到后的未来区段重算、限电周期推进与关闭、恢复运行后的
延续排程，以及燃料与能量账目闭合。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polar_station_foundation.storage import Database

from .clock import ManualClock
from .planner import SLOT_MINUTES
from .service import EnergyService

T0 = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)


def _slot(value: datetime) -> int:
    return int(value.timestamp()) // (SLOT_MINUTES * 60)


def run() -> dict[str, object]:
    """执行完整场景并返回验收结果，任一断言失败都会抛出异常。"""

    checks: list[str] = []

    def check(name: str, condition: bool) -> None:
        if not condition:
            raise AssertionError(f"验收失败: {name}")
        checks.append(name)

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "energy.sqlite3"
        database = Database(path)
        clock = ManualClock(T0)
        service = EnergyService(database, clock)
        s0 = _slot(T0)

        # ---- 建档：机构、两名值班工程师、场所 ----
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="极地科考机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-op1", actor_id="admin-001", new_actor_id="op-001",
                               display_name="值班工程师甲", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-op2", actor_id="admin-001", new_actor_id="op-002",
                               display_name="值班工程师乙", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="admin-001", site_id="st-001",
                              organization_id="org-001", name="极夜科考站", timezone_name="UTC")

        # ---- 能源资产与回路 ----
        service.register_generator(request_id="req-gen", actor_id="op-001", site_id="st-001",
                                   generator_id="gen-01", rated_kw=60.0, min_kw=10.0,
                                   liters_per_kwh=0.25)
        service.register_battery(request_id="req-bat", actor_id="op-001", site_id="st-001",
                                 battery_id="bat-01", capacity_kwh=40.0, max_charge_kw=20.0,
                                 max_discharge_kw=20.0, efficiency=0.95, initial_soc_kwh=20.0)
        service.register_wind_turbine(request_id="req-wind", actor_id="op-001", site_id="st-001",
                                      wind_id="wind-01", rated_kw=30.0)
        service.register_fuel_batch(request_id="req-f1", actor_id="op-001", site_id="st-001",
                                    batch_id="fuel-a", liters=80.0, available_slot=s0)
        service.register_fuel_batch(request_id="req-f2", actor_id="op-001", site_id="st-001",
                                    batch_id="fuel-b", liters=160.0, available_slot=s0 + 24)
        circuits = [
            ("med-01", "医疗舱", "medical", 4.0, None, [], 0, 0.0, 0),
            ("comm-01", "卫星通信", "comms", 2.0, None, [], 0, 0.0, 0),
            ("heat-01", "管线伴热", "antifreeze", 6.0, None, [], 0, 0.0, 0),
            ("life-01", "生命保障", "life_support", 8.0, None, [], 0, 0.0, 90),
            ("lab-01", "冰芯实验", "experiment", 10.0, None, [], 4, 3.0, 50),
            ("work-01", "维修车间", "general", 7.0, None, [], 2, 1.0, 30),
            ("green-01", "温室", "general", 6.0, None, ["work-01"], 0, 0.0, 40),
            ("galley-01", "厨房", "comfort", 5.0, "hab", [], 0, 0.0, 20),
            ("laundry-01", "洗衣房", "comfort", 3.0, "hab", [], 0, 0.0, 10),
        ]
        for index, (cid, name, tier, kw, group, deps, run_slots, startup, rank) in enumerate(circuits):
            service.register_circuit(request_id=f"req-c{index}", actor_id="op-001", site_id="st-001",
                                     circuit_id=cid, name=name, tier=tier, default_kw=kw,
                                     group_id=group, depends_on=deps, min_run_slots=run_slots,
                                     startup_kwh=startup, shed_rank=rank)
        service.approve_experiment_window(request_id="req-win", actor_id="op-001", site_id="st-001",
                                          window_id="win-01", circuit_id="lab-01",
                                          start_slot=s0 + 8, end_slot=s0 + 16)
        for slot in range(s0, s0 + 121):
            service.set_forecast(request_id=f"req-fc-{slot}", actor_id="op-001",
                                 site_id="st-001", slot=slot, wind_kw=0.0)

        def directive(plan_id: str, circuit_id: str, slot: int) -> dict:
            for row in service.plan_directives(plan_id, slot):
                if row["circuit_id"] == circuit_id:
                    return row
            raise AssertionError(f"缺少指令 {plan_id}/{circuit_id}/{slot}")

        # ---- 第一轮承诺：双人确认封存 ----
        plan1 = service.create_plan(request_id="req-p1", actor_id="op-001",
                                    site_id="st-001", horizon_slots=48).resource_id
        service.confirm_plan(request_id="req-p1-c1", actor_id="op-001", plan_id=plan1)
        check("单次确认不封存", service.get_plan(plan1)["state"] == "draft")
        service.confirm_plan(request_id="req-p1-c2", actor_id="op-002", plan_id=plan1)
        check("双人确认后封存", service.get_plan(plan1)["state"] == "sealed")
        for slot in (s0, s0 + 10, s0 + 30, s0 + 47):
            for cid in ("med-01", "comm-01", "heat-01"):
                row = directive(plan1, cid, slot)
                check(f"保障回路 {cid} 时段 {slot} 供电", row["state"] == "on"
                      and row["reason_code"] == "tier_must_keep")
        check("实验窗口内供电", directive(plan1, "lab-01", s0 + 10)["state"] == "on")
        check("实验窗口依据", directive(plan1, "lab-01", s0 + 8)["reason_code"] == "experiment_window")
        check("最低运行时长守护", directive(plan1, "lab-01", s0 + 9)["reason_code"] == "min_run_active")
        check("窗口外不供电", directive(plan1, "lab-01", s0 + 20)["reason_code"] == "outside_window")
        check("温室依赖车间", directive(plan1, "work-01", s0)["state"] == "on")
        galley_late = directive(plan1, "galley-01", s0 + 20)
        laundry_late = directive(plan1, "laundry-01", s0 + 20)
        check("燃料储备切除舒适负荷", galley_late["state"] == "off"
              and galley_late["reason_code"] == "shed_fuel_reserve")
        check("成组设备整组切除", laundry_late["state"] == "off"
              and laundry_late["reason_code"] == "shed_fuel_reserve")
        episode = service.curtailment_status("st-001")["open_episode"]
        check("限电周期已开启", episode is not None)

        # ---- 遥测入账与幂等结算 ----
        clock.set(T0 + timedelta(hours=2))
        dispatch1 = {row["slot"]: row for row in service.get_plan(plan1)["dispatch"]}
        for index, slot in enumerate(range(s0, s0 + 4)):
            row = dispatch1[slot]
            service.ingest_telemetry(
                request_id=f"req-t{index}", actor_id="op-001", site_id="st-001", slot=slot,
                generation_kwh=row["gen_kw"] * 0.5, wind_kwh=row["wind_kw"] * 0.5,
                load_kwh=row["demand_kw"] * 0.5, battery_flow_kwh=row["battery_kw"] * 0.5)
        check("四个时段完成结算", len(service.settlements("st-001")) == 4)
        first = dispatch1[s0]
        replay = service.ingest_telemetry(
            request_id="req-t0", actor_id="op-001", site_id="st-001", slot=s0,
            generation_kwh=first["gen_kw"] * 0.5, wind_kwh=first["wind_kw"] * 0.5,
            load_kwh=first["demand_kw"] * 0.5, battery_flow_kwh=first["battery_kw"] * 0.5)
        check("遥测重放返回原回执", replay.replayed)
        check("重放不重复结算", len(service.settlements("st-001")) == 4)
        again = service.ingest_telemetry(
            request_id="req-t0b", actor_id="op-001", site_id="st-001", slot=s0,
            generation_kwh=first["gen_kw"] * 0.5, wind_kwh=first["wind_kw"] * 0.5,
            load_kwh=first["demand_kw"] * 0.5, battery_flow_kwh=first["battery_kw"] * 0.5)
        check("相同时段同一结算单", again.resource_id == replay.resource_id)
        check("结算后账目闭合", service.reconcile("st-001")["closed"])

        # ---- 补给迟到：只重算未来区段 ----
        service.delay_fuel_batch(request_id="req-delay", actor_id="op-001", site_id="st-001",
                                 batch_id="fuel-b", available_slot=s0 + 40)
        plan1_directives = len(service.plan_directives(plan1))
        plan2 = service.create_plan(request_id="req-p2", actor_id="op-001",
                                    site_id="st-001", horizon_slots=48).resource_id
        service.confirm_plan(request_id="req-p2-c1", actor_id="op-001", plan_id=plan2)
        service.confirm_plan(request_id="req-p2-c2", actor_id="op-002", plan_id=plan2)
        check("新方案从当前时段起算", service.get_plan(plan2)["base_slot"] == s0 + 4)
        check("原方案已被取代", service.get_plan(plan1)["state"] == "superseded")
        check("已执行指令保持原样", len(service.plan_directives(plan1)) == plan1_directives == 9 * 48)
        check("新方案不改写过去", all(row["slot"] >= s0 + 4
                                      for row in service.plan_directives(plan2)))
        check("迟到燃料收紧舒适负荷", directive(plan2, "galley-01", s0 + 20)["reason_code"]
              == "shed_fuel_reserve")
        check("迟到燃料收紧生活保障", directive(plan2, "life-01", s0 + 30)["reason_code"]
              == "shed_fuel_reserve")
        check("限电周期持续推进", service.curtailment_status("st-001")["open_episode"] is not None)

        # ---- 发电机故障：储能耗尽前守住保障负荷 ----
        service.report_failure(request_id="req-fail", actor_id="op-001", site_id="st-001",
                               kind="generator", asset_id="gen-01")
        plan3 = service.create_plan(request_id="req-p3", actor_id="op-001",
                                    site_id="st-001", horizon_slots=48).resource_id
        service.confirm_plan(request_id="req-p3-c1", actor_id="op-001", plan_id=plan3)
        service.confirm_plan(request_id="req-p3-c2", actor_id="op-002", plan_id=plan3)
        check("故障方案标记失守", service.get_plan(plan3)["breached"])
        check("故障后功率缺口切除", directive(plan3, "green-01", s0 + 4)["reason_code"]
              == "shed_deficit")
        breach_actions = [a for a in service.curtailment_status("st-001")["actions"]
                          if a["action"] == "breach"]
        check("失守动作记入限电周期", len(breach_actions) > 0)
        service.clear_failure(request_id="req-fix", actor_id="op-001", site_id="st-001",
                              kind="generator", asset_id="gen-01")
        plan4 = service.create_plan(request_id="req-p4", actor_id="op-001",
                                    site_id="st-001", horizon_slots=48).resource_id
        service.confirm_plan(request_id="req-p4-c1", actor_id="op-001", plan_id=plan4)
        service.confirm_plan(request_id="req-p4-c2", actor_id="op-002", plan_id=plan4)
        check("恢复后方案不再失守", not service.get_plan(plan4)["breached"])

        # ---- 人工覆盖：双人确认且期限内有效 ----
        override = service.create_override(request_id="req-ov", actor_id="op-001", site_id="st-001",
                                           circuit_id="galley-01", slot=s0 + 20, desired_state="on",
                                           reason="医疗餐制备", ttl_slots=20).resource_id
        service.confirm_override(request_id="req-ov-c1", actor_id="op-001", override_id=override)
        plan5 = service.create_plan(request_id="req-p5", actor_id="op-001",
                                    site_id="st-001", horizon_slots=48).resource_id
        check("单人确认覆盖不生效", directive(plan5, "galley-01", s0 + 20)["state"] == "off")
        service.confirm_override(request_id="req-ov-c2", actor_id="op-002", override_id=override)
        plan5b = service.create_plan(request_id="req-p5b", actor_id="op-001",
                                     site_id="st-001", horizon_slots=48).resource_id
        check("双人确认后覆盖生效", directive(plan5b, "galley-01", s0 + 20)["reason_code"]
              == "override_forced_on")
        check("覆盖带动整组供电", directive(plan5b, "laundry-01", s0 + 20)["reason_code"]
              == "group_required")
        service.confirm_plan(request_id="req-p5b-c1", actor_id="op-001", plan_id=plan5b)
        service.confirm_plan(request_id="req-p5b-c2", actor_id="op-002", plan_id=plan5b)

        # ---- 风能与燃料恢复：限电周期关闭 ----
        for slot in range(s0 + 4, s0 + 121):
            service.set_forecast(request_id=f"req-fw-{slot}", actor_id="op-001",
                                 site_id="st-001", slot=slot, wind_kw=25.0)
        service.register_fuel_batch(request_id="req-f3", actor_id="op-001", site_id="st-001",
                                    batch_id="fuel-c", liters=400.0, available_slot=s0 + 4)
        plan6 = service.create_plan(request_id="req-p6", actor_id="op-001",
                                    site_id="st-001", horizon_slots=48).resource_id
        service.confirm_plan(request_id="req-p6-c1", actor_id="op-001", plan_id=plan6)
        service.confirm_plan(request_id="req-p6-c2", actor_id="op-002", plan_id=plan6)
        shed_codes = {"shed_deficit", "shed_fuel_reserve", "shed_dependency", "shed_group"}
        check("恢复后无切除指令", all(row["reason_code"] not in shed_codes
                                      for row in service.plan_directives(plan6)))
        status = service.curtailment_status("st-001")
        check("限电周期关闭", status["open_episode"] is None and status["closed_episodes"] >= 1)

        # ---- 恢复后继续结算，账目仍然闭合 ----
        clock.set(T0 + timedelta(hours=3))
        dispatch6 = {row["slot"]: row for row in service.get_plan(plan6)["dispatch"]}
        for index, slot in enumerate((s0 + 4, s0 + 5)):
            row = dispatch6[slot]
            service.ingest_telemetry(
                request_id=f"req-tx{index}", actor_id="op-001", site_id="st-001", slot=slot,
                generation_kwh=row["gen_kw"] * 0.5, wind_kwh=row["wind_kw"] * 0.5,
                load_kwh=row["demand_kw"] * 0.5, battery_flow_kwh=row["battery_kw"] * 0.5)
        reconciliation = service.reconcile("st-001")
        check("燃料账目闭合", reconciliation["fuel"]["closed"])
        check("能量账目闭合", reconciliation["energy"]["closed"])
        database.close()

        # ---- 服务恢复运行：未结束的承诺继续推进 ----
        clock.set(T0 + timedelta(hours=30))
        reopened = Database(path)
        service2 = EnergyService(reopened, clock)
        resumed = service2.resume_site(request_id="req-resume", actor_id="op-001",
                                       site_id="st-001", horizon_slots=48)
        check("恢复运行生成延续方案", not resumed.replayed)
        plan7 = resumed.resource_id
        check("延续方案覆盖当前时段", service2.get_plan(plan7)["base_slot"] == s0 + 60)
        service2.confirm_plan(request_id="req-p7-c1", actor_id="op-001", plan_id=plan7)
        service2.confirm_plan(request_id="req-p7-c2", actor_id="op-002", plan_id=plan7)
        check("延续方案封存", service2.get_plan(plan7)["state"] == "sealed")
        check("延续方案守住保障回路", all(
            row["state"] == "on" for row in service2.plan_directives(plan7, s0 + 70)
            if row["circuit_id"] in ("med-01", "comm-01", "heat-01")))
        check("恢复后限电周期未重开", service2.curtailment_status("st-001")["open_episode"] is None)
        valid, event_count = service2.verify_audit()
        check("审计链完整", valid)
        check("重启后账目仍闭合", service2.reconcile("st-001")["closed"])
        overview = service2.site_overview("st-001")
        check("总览给出回路依据", len(overview["circuits"]) == 9
              and all("reason_code" in row for row in overview["circuits"]))
        settlements_count = len(service2.settlements("st-001"))
        plans_count = len(service2.list_plans("st-001"))
        reopened.close()

        return {
            "status": "ok",
            "checks": len(checks),
            "audit_events": event_count,
            "audit_valid": valid,
            "settlements": settlements_count,
            "fuel_remaining_liters": reconciliation["fuel"]["remaining_liters"],
            "plans": plans_count,
        }


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
