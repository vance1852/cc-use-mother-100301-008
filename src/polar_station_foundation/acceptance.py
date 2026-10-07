"""运行基础服务与能源承诺处置的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .energy_service import EnergyService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链与能源承诺处置链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = EnergyService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范科考机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="站务负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号科考站点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="station_operator_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="station_operator_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        service.register_generator(request_id="req-gen", actor_id="operator-001", site_id="site-001",
                                   generator_id="gen-001", name="主柴油发电机", rated_kw=50.0,
                                   fuel_type="diesel", liters_per_kwh=0.3)
        service.register_fuel_batch(request_id="req-fuel", actor_id="operator-001", site_id="site-001",
                                    batch_id="fuel-001", fuel_type="diesel", quantity_liters=500.0,
                                    available_from="2026-09-25T00:00:00Z")
        service.register_battery(request_id="req-bat", actor_id="operator-001", site_id="site-001",
                                 battery_id="bat-001", capacity_kwh=100.0, max_charge_kw=40.0,
                                 max_discharge_kw=40.0, soc_kwh=50.0, efficiency=1.0)
        service.register_circuit(request_id="req-c1", actor_id="operator-001", site_id="site-001",
                                 circuit_id="cir-med", name="医疗舱", protection_level=1,
                                 demand_kw=10.0, state="on")
        service.register_circuit(request_id="req-c2", actor_id="operator-001", site_id="site-001",
                                 circuit_id="cir-comms", name="通信台", protection_level=2,
                                 demand_kw=5.0, state="on")
        service.register_circuit(request_id="req-c3", actor_id="operator-001", site_id="site-001",
                                 circuit_id="cir-anti", name="防冻加热", protection_level=3,
                                 demand_kw=15.0, state="on")
        service.register_circuit(request_id="req-c4", actor_id="operator-001", site_id="site-001",
                                 circuit_id="cir-exp", name="实验舱", protection_level=5,
                                 demand_kw=40.0)
        service.register_circuit(request_id="req-c5", actor_id="operator-001", site_id="site-001",
                                 circuit_id="cir-comfort", name="生活照明", protection_level=6,
                                 demand_kw=30.0, state="on")
        plan = service.compute_plan(request_id="req-plan", actor_id="operator-001",
                                    site_id="site-001", horizon_intervals=12, interval_minutes=60)
        service.seal_plan(request_id="req-seal", actor_id="operator-001", plan_id=plan.resource_id)
        service.record_telemetry(request_id="req-tm", actor_id="operator-001", site_id="site-001",
                                 telemetry_id="tm-001", interval_start="2026-09-25T08:00:00Z",
                                 interval_end="2026-09-25T09:00:00Z",
                                 generators=[{"generator_id": "gen-001", "kwh": 50.0}],
                                 battery_soc_kwh=30.0,
                                 circuits=[{"circuit_id": "cir-med", "kwh": 10.0, "state": "on"},
                                           {"circuit_id": "cir-comms", "kwh": 5.0, "state": "on"},
                                           {"circuit_id": "cir-anti", "kwh": 15.0, "state": "on"},
                                           {"circuit_id": "cir-exp", "kwh": 40.0, "state": "on"},
                                           {"circuit_id": "cir-comfort", "kwh": 0.0, "state": "off"}])
        state = service.get_dispatch_state("site-001")
        account = service.verify_energy_account("site-001")
        rationale = service.get_plan_rationale(plan.resource_id)

        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "energy_plan_sealed": state["active_plan"] is not None,
                  "energy_curtailments": len(state["curtailments"]),
                  "energy_rationale_circuits": len(rationale["circuits"]),
                  "energy_account_closed": account["closed"]}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"]
          and result["energy_plan_sealed"] and result["energy_account_closed"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
