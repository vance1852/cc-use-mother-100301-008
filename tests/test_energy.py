"""能源承诺与负荷处置系统的端到端规则测试。"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polar_station_foundation.energy_service import EnergyService
from polar_station_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from polar_station_foundation.storage import Database


T0 = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


class MutableClock:
    """可在测试中推进的时间基准。"""

    def __init__(self, value=T0):
        self.value = value

    def now(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


def make_service(database=None, clock=None):
    database = database or Database()
    clock = clock or MutableClock()
    service = EnergyService(database, clock)
    service.register_organization(request_id="org", actor_id="bootstrap",
                                  organization_id="o1", name="极地科考机构")
    service.register_actor(request_id="a1", actor_id="bootstrap", new_actor_id="admin",
                           display_name="系统管理员", role="admin", organization_id="o1")
    service.register_actor(request_id="a2", actor_id="admin", new_actor_id="op1",
                           display_name="值班工程师一", role="operator", organization_id="o1")
    service.register_actor(request_id="a3", actor_id="admin", new_actor_id="op2",
                           display_name="值班工程师二", role="operator", organization_id="o1")
    service.register_actor(request_id="a4", actor_id="admin", new_actor_id="rev1",
                           display_name="实验审核员", role="reviewer", organization_id="o1")
    service.register_site(request_id="site1", actor_id="op1", site_id="site",
                          organization_id="o1", name="极夜科考站", timezone_name="UTC")
    return service


def add_generator(service, gen="g1", rated=100.0, liters=0.3, fuel="diesel"):
    return service.register_generator(request_id=f"gen-{gen}", actor_id="op1", site_id="site",
                                      generator_id=gen, name=f"发电机{gen}", rated_kw=rated,
                                      fuel_type=fuel, liters_per_kwh=liters)


def add_fuel(service, batch="f1", quantity=2000.0, available=T0, fuel="diesel"):
    return service.register_fuel_batch(request_id=f"fuel-{batch}", actor_id="op1",
                                       site_id="site", batch_id=batch, fuel_type=fuel,
                                       quantity_liters=quantity, available_from=iso(available))


def add_battery(service, capacity=200.0, charge=50.0, discharge=50.0, soc=100.0, eff=0.95):
    return service.register_battery(request_id="bat-b1", actor_id="op1", site_id="site",
                                    battery_id="b1", capacity_kwh=capacity,
                                    max_charge_kw=charge, max_discharge_kw=discharge,
                                    soc_kwh=soc, efficiency=eff)


def add_circuit(service, circuit_id, level, demand, **kwargs):
    return service.register_circuit(request_id=f"cir-{circuit_id}", actor_id="op1",
                                    site_id="site", circuit_id=circuit_id,
                                    name=f"回路{circuit_id}", protection_level=level,
                                    demand_kw=demand, **kwargs)


def decisions_of(service, plan_id):
    """把方案依据整理成 decisions[circuit][interval] = (decision, code, detail)。"""
    rationale = service.get_plan_rationale(plan_id)
    result = {}
    for circuit in rationale["circuits"]:
        result[circuit["circuit_id"]] = {
            item["interval_index"]: (item["decision"], item["reason_code"],
                                     item["reason_detail"])
            for item in circuit["intervals"]
        }
    return result, rationale["resources"]


class ForecastTest(unittest.TestCase):
    """预测把发电、储能、燃料批次和分时需求连成逐区间结果。"""

    def setUp(self):
        self.database = Database()
        self.clock = MutableClock()
        self.service = make_service(self.database, self.clock)

    def tearDown(self):
        self.database.close()

    def test_forecast_links_generation_storage_fuel_and_demand(self):
        add_generator(self.service, rated=150.0)
        add_fuel(self.service, quantity=2000.0)
        add_battery(self.service)
        add_circuit(self.service, "med", 1, 10.0, state="on")
        add_circuit(self.service, "comms", 2, 5.0, state="on")
        add_circuit(self.service, "anti", 3, 20.0, state="on")
        add_circuit(self.service, "life", 4, 15.0, state="on")
        add_circuit(self.service, "h1", 4, 10.0, group_id="gh", state="on")
        add_circuit(self.service, "h2", 4, 10.0, group_id="gh", state="on")
        add_circuit(self.service, "exp", 5, 30.0)
        add_circuit(self.service, "comfort", 6, 25.0, state="on")
        plan = self.service.compute_plan(request_id="p1", actor_id="op1", site_id="site",
                                         horizon_intervals=24, interval_minutes=60)
        _, resources = decisions_of(self.service, plan.resource_id)
        self.assertEqual(24, len(resources))
        first = resources[0]
        self.assertAlmostEqual(125.0, first["demand_kwh"])
        self.assertAlmostEqual(0.0, first["discharge_kwh"])
        self.assertAlmostEqual(25.0, first["charge_kwh"])
        self.assertAlmostEqual(100.0 + 25.0 * 0.95, first["soc_kwh"])
        self.assertAlmostEqual(150.0 * 0.3, first["fuel_liters"])
        self.assertAlmostEqual(0.0, first["deficit_kwh"])

    def test_profile_drives_time_of_use_demand(self):
        add_generator(self.service, rated=200.0)
        add_fuel(self.service)
        add_circuit(self.service, "med", 1, 10.0, state="on")
        add_circuit(self.service, "climate", 4, 10.0, state="on",
                    profile=[{"start_hour": 8, "end_hour": 20, "kw": 40.0}])
        plan = self.service.compute_plan(request_id="p1", actor_id="op1", site_id="site",
                                         horizon_intervals=24, interval_minutes=60)
        _, resources = decisions_of(self.service, plan.resource_id)
        self.assertAlmostEqual(50.0, resources[0]["demand_kwh"])
        self.assertAlmostEqual(20.0, resources[12]["demand_kwh"])

    def test_startup_cost_is_counted_in_interval_demand(self):
        add_generator(self.service, rated=100.0)
        add_fuel(self.service)
        add_circuit(self.service, "med", 1, 10.0, state="on")
        add_circuit(self.service, "exp", 5, 30.0, startup_kwh=6.0)
        plan = self.service.compute_plan(request_id="p1", actor_id="op1", site_id="site",
                                         horizon_intervals=6, interval_minutes=60)
        _, resources = decisions_of(self.service, plan.resource_id)
        self.assertAlmostEqual(46.0, resources[0]["demand_kwh"])
        self.assertAlmostEqual(40.0, resources[1]["demand_kwh"])


class DeficitTest(unittest.TestCase):
    """缺口下先守住医疗、通信和防冻，成组回路整体切除。"""

    def setUp(self):
        self.database = Database()
        self.clock = MutableClock()
        self.service = make_service(self.database, self.clock)
        add_generator(self.service, rated=100.0, liters=0.3)
        add_fuel(self.service, quantity=30.0)
        add_battery(self.service, capacity=200.0, charge=50.0, discharge=50.0,
                    soc=100.0, eff=1.0)
        add_circuit(self.service, "med", 1, 10.0, state="on")
        add_circuit(self.service, "comms", 2, 5.0, state="on")
        add_circuit(self.service, "anti", 3, 20.0, state="on")
        add_circuit(self.service, "life", 4, 15.0, state="on")
        add_circuit(self.service, "h1", 4, 10.0, group_id="gh", state="on")
        add_circuit(self.service, "h2", 4, 10.0, group_id="gh", state="on")
        add_circuit(self.service, "exp", 5, 30.0)
        add_circuit(self.service, "comfort", 6, 25.0, state="on")
        self.plan = self.service.compute_plan(request_id="p1", actor_id="op1",
                                              site_id="site", horizon_intervals=6,
                                              interval_minutes=60)
        self.decisions, self.resources = decisions_of(self.service, self.plan.resource_id)

    def tearDown(self):
        self.database.close()

    def test_protected_levels_stay_on_under_deficit(self):
        self.assertEqual(("on", "protected_medical"), self.decisions["med"][1][:2])
        self.assertEqual(("on", "protected_comms"), self.decisions["comms"][1][:2])
        self.assertEqual(("on", "protected_antifreeze"), self.decisions["anti"][1][:2])
        self.assertEqual("on", self.decisions["med"][2][0])
        self.assertGreater(self.resources[2]["deficit_kwh"], 0.0)

    def test_shedding_follows_protection_order(self):
        self.assertEqual("off", self.decisions["comfort"][1][0])
        self.assertEqual("deficit_shed", self.decisions["comfort"][1][1])
        self.assertEqual("off", self.decisions["exp"][1][0])
        self.assertEqual("on", self.decisions["life"][1][0])

    def test_group_is_shed_as_a_whole(self):
        for index in range(6):
            self.assertEqual(self.decisions["h1"][index][0], self.decisions["h2"][index][0])
        self.assertEqual("off", self.decisions["h1"][1][0])
        self.assertIn("成组回路 gh 整体切除", self.decisions["h1"][1][2])
        self.assertIn("成组回路 gh 整体切除", self.decisions["h2"][1][2])

    def test_fuel_exhaustion_is_alarmd(self):
        rationale = self.service.get_plan_rationale(self.plan.resource_id)
        self.assertTrue(any("燃料" in alarm and "耗尽" in alarm
                            for alarm in rationale["plan"]["alarms"]))


class ConstraintTest(unittest.TestCase):
    """依赖、最低运行时长、实验窗口与人工覆盖。"""

    def setUp(self):
        self.database = Database()
        self.clock = MutableClock()
        self.service = make_service(self.database, self.clock)

    def tearDown(self):
        self.database.close()

    def test_dependency_cascades_shed_to_children(self):
        add_generator(self.service, rated=20.0)
        add_fuel(self.service)
        add_circuit(self.service, "med", 1, 10.0, state="on")
        add_circuit(self.service, "main", 5, 30.0)
        add_circuit(self.service, "sub", 4, 5.0, depends_on="main")
        plan = self.service.compute_plan(request_id="p1", actor_id="op1", site_id="site",
                                         horizon_intervals=3, interval_minutes=60)
        decisions, _ = decisions_of(self.service, plan.resource_id)
        self.assertEqual("off", decisions["main"][0][0])
        self.assertEqual("deficit_shed", decisions["main"][0][1])
        self.assertEqual("off", decisions["sub"][0][0])
        self.assertEqual("dependency_parent_off", decisions["sub"][0][1])

    def test_dependency_rejects_unknown_parent(self):
        with self.assertRaises(NotFoundError):
            add_circuit(self.service, "orphan", 5, 5.0, depends_on="missing")

    def test_min_run_keeps_circuit_on_then_releases(self):
        add_generator(self.service, rated=25.0)
        add_fuel(self.service)
        add_circuit(self.service, "med", 1, 10.0, state="on")
        add_circuit(self.service, "freezer", 5, 20.0, state="on", min_run_intervals=3)
        add_circuit(self.service, "comfort", 6, 15.0, state="on")
        plan = self.service.compute_plan(request_id="p1", actor_id="op1", site_id="site",
                                         horizon_intervals=6, interval_minutes=60)
        decisions, resources = decisions_of(self.service, plan.resource_id)
        self.assertEqual(("on", "min_run"), decisions["freezer"][0][:2])
        self.assertEqual(("on", "min_run"), decisions["freezer"][2][:2])
        self.assertAlmostEqual(5.0, resources[0]["deficit_kwh"])
        self.assertEqual("off", decisions["freezer"][3][0])

    def test_experiment_window_is_protected(self):
        add_generator(self.service, rated=25.0)
        add_fuel(self.service)
        add_circuit(self.service, "med", 1, 10.0, state="on")
        add_circuit(self.service, "exp", 5, 30.0)
        with self.assertRaises(PermissionDenied):
            self.service.approve_experiment_window(
                request_id="w0", actor_id="op1", site_id="site", window_id="w1",
                circuit_id="exp", start=iso(T0 + timedelta(hours=2)),
                end=iso(T0 + timedelta(hours=4)), required_kw=30.0)
        self.service.approve_experiment_window(
            request_id="w1", actor_id="rev1", site_id="site", window_id="w1",
            circuit_id="exp", start=iso(T0 + timedelta(hours=2)),
            end=iso(T0 + timedelta(hours=4)), required_kw=30.0)
        plan = self.service.compute_plan(request_id="p1", actor_id="op1", site_id="site",
                                         horizon_intervals=6, interval_minutes=60)
        decisions, resources = decisions_of(self.service, plan.resource_id)
        self.assertEqual("off", decisions["exp"][0][0])
        self.assertEqual(("on", "experiment_window"), decisions["exp"][2][:2])
        self.assertAlmostEqual(15.0, resources[2]["deficit_kwh"])
        self.assertEqual("off", decisions["exp"][4][0])

    def test_override_requires_dual_confirmation(self):
        add_generator(self.service, rated=30.0)
        add_fuel(self.service)
        add_circuit(self.service, "med", 1, 10.0, state="on")
        add_circuit(self.service, "comfort", 6, 25.0, state="on")
        self.service.request_override(request_id="o1", actor_id="op1", site_id="site",
                                      override_id="ov1", circuit_id="comfort",
                                      action="force_on", valid_from=iso(T0),
                                      valid_until=iso(T0 + timedelta(hours=6)))
        with self.assertRaises(PermissionDenied):
            self.service.confirm_override(request_id="o1c", actor_id="op1",
                                          override_id="ov1")
        plan = self.service.compute_plan(request_id="p1", actor_id="op1", site_id="site",
                                         horizon_intervals=3, interval_minutes=60)
        decisions, _ = decisions_of(self.service, plan.resource_id)
        self.assertEqual("off", decisions["comfort"][0][0])
        self.service.confirm_override(request_id="o2c", actor_id="op2", override_id="ov1")
        plan2 = self.service.compute_plan(request_id="p2", actor_id="op1", site_id="site",
                                          horizon_intervals=3, interval_minutes=60)
        decisions2, resources2 = decisions_of(self.service, plan2.resource_id)
        self.assertEqual(("on", "override_force_on"), decisions2["comfort"][0][:2])
        self.assertAlmostEqual(5.0, resources2[0]["deficit_kwh"])

    def test_override_only_valid_within_window(self):
        add_generator(self.service, rated=30.0)
        add_fuel(self.service)
        add_circuit(self.service, "med", 1, 10.0, state="on")
        add_circuit(self.service, "comfort", 6, 25.0, state="on")
        self.service.request_override(request_id="o1", actor_id="op1", site_id="site",
                                      override_id="ov1", circuit_id="comfort",
                                      action="force_on",
                                      valid_from=iso(T0 + timedelta(hours=2)),
                                      valid_until=iso(T0 + timedelta(hours=3)))
        self.service.confirm_override(request_id="o1c", actor_id="op2", override_id="ov1")
        plan = self.service.compute_plan(request_id="p1", actor_id="op1", site_id="site",
                                         horizon_intervals=5, interval_minutes=60)
        decisions, _ = decisions_of(self.service, plan.resource_id)
        self.assertEqual("off", decisions["comfort"][1][0])
        self.assertEqual(("on", "override_force_on"), decisions["comfort"][2][:2])
        self.assertEqual("off", decisions["comfort"][3][0])

    def test_expired_override_cannot_be_confirmed(self):
        add_circuit(self.service, "comfort", 6, 25.0, state="on")
        self.service.request_override(request_id="o1", actor_id="op1", site_id="site",
                                      override_id="ov1", circuit_id="comfort",
                                      action="force_on",
                                      valid_from=iso(T0 - timedelta(hours=2)),
                                      valid_until=iso(T0 - timedelta(hours=1)))
        with self.assertRaises(ValidationError):
            self.service.confirm_override(request_id="o1c", actor_id="op2",
                                          override_id="ov1")

    def test_protected_circuit_cannot_be_forced_off(self):
        add_circuit(self.service, "med", 1, 10.0, state="on")
        with self.assertRaises(ValidationError):
            self.service.request_override(request_id="o1", actor_id="op1", site_id="site",
                                          override_id="ov1", circuit_id="med",
                                          action="force_off", valid_from=iso(T0),
                                          valid_until=iso(T0 + timedelta(hours=1)))


class PlanLifecycleTest(unittest.TestCase):
    """封存唯一性、未来区段重算与恢复运行。"""

    def setUp(self):
        self.database = Database()
        self.clock = MutableClock()
        self.service = make_service(self.database, self.clock)
        add_generator(self.service, rated=150.0)
        add_fuel(self.service, quantity=200.0)
        add_battery(self.service)
        add_circuit(self.service, "med", 1, 10.0, state="on")
        add_circuit(self.service, "comms", 2, 5.0, state="on")
        add_circuit(self.service, "anti", 3, 20.0, state="on")
        add_circuit(self.service, "life", 4, 15.0, state="on")
        add_circuit(self.service, "h1", 4, 10.0, group_id="gh", state="on")
        add_circuit(self.service, "h2", 4, 10.0, group_id="gh", state="on")
        add_circuit(self.service, "exp", 5, 30.0)
        add_circuit(self.service, "comfort", 6, 25.0, state="on")

    def tearDown(self):
        self.database.close()

    def test_only_one_plan_is_sealed_per_horizon(self):
        first = self.service.compute_plan(request_id="p1", actor_id="op1", site_id="site")
        second = self.service.compute_plan(request_id="p2", actor_id="op1", site_id="site")
        sealed = self.service.seal_plan(request_id="seal1", actor_id="op1",
                                        plan_id=first.resource_id)
        self.assertFalse(sealed.replayed)
        with self.assertRaises(ConflictError):
            self.service.seal_plan(request_id="seal2", actor_id="op1",
                                   plan_id=second.resource_id)
        again = self.service.seal_plan(request_id="seal3", actor_id="op2",
                                       plan_id=first.resource_id)
        self.assertEqual(first.resource_id, again.resource_id)
        replay = self.service.seal_plan(request_id="seal1", actor_id="op1",
                                        plan_id=first.resource_id)
        self.assertTrue(replay.replayed)

    def test_replan_keeps_executed_intervals_and_recomputes_future(self):
        plan = self.service.compute_plan(request_id="p1", actor_id="op1", site_id="site")
        self.service.seal_plan(request_id="seal1", actor_id="op1", plan_id=plan.resource_id)
        old_decisions, _ = decisions_of(self.service, plan.resource_id)
        old_count = sum(len(items) for items in old_decisions.values())
        self.clock.advance(hours=2)
        follow = self.service.replan(request_id="r1", actor_id="op1", site_id="site",
                                     trigger="power_change", note="实测功率偏离")
        self.assertEqual("energy_plan", follow.resource_type)
        plans = {item["plan_id"]: item for item in self.service.list_plans("site")}
        self.assertEqual("superseded", plans[plan.resource_id]["status"])
        self.assertEqual("sealed", plans[follow.resource_id]["status"])
        self.assertEqual(plan.resource_id, plans[follow.resource_id]["based_on"])
        new_decisions, _ = decisions_of(self.service, follow.resource_id)
        for circuit_id, items in old_decisions.items():
            executed = items[2]
            inherited = new_decisions[circuit_id][0]
            self.assertEqual(executed[0], inherited[0])
            self.assertTrue(inherited[2].startswith("已执行指令保留"))
        kept, _ = decisions_of(self.service, plan.resource_id)
        self.assertEqual(old_count, sum(len(items) for items in kept.values()))

    def test_equipment_fault_replans_future_only(self):
        plan = self.service.compute_plan(request_id="p1", actor_id="op1", site_id="site")
        self.service.seal_plan(request_id="seal1", actor_id="op1", plan_id=plan.resource_id)
        self.clock.advance(hours=1)
        self.service.report_equipment_status(request_id="f1", actor_id="op1",
                                             site_id="site", equipment_type="generator",
                                             equipment_id="g1", status="fault")
        plans = [item for item in self.service.list_plans("site")
                 if item["status"] == "sealed"]
        self.assertEqual(1, len(plans))
        self.assertEqual("equipment_status", plans[0]["trigger"])
        decisions, resources = decisions_of(self.service, plans[0]["plan_id"])
        self.assertEqual("on", decisions["comfort"][0][0])
        self.assertTrue(decisions["comfort"][0][2].startswith("已执行指令保留"))
        self.assertEqual("off", decisions["comfort"][1][0])
        self.assertAlmostEqual(0.0, resources[1]["generation_kwh"])
        old_decisions, _ = decisions_of(self.service, plan.resource_id)
        self.assertEqual("on", old_decisions["comfort"][0][0])

    def test_fuel_delay_replans_with_later_availability(self):
        add_fuel(self.service, batch="f2", quantity=300.0,
                 available=T0 + timedelta(hours=10))
        plan = self.service.compute_plan(request_id="p1", actor_id="op1", site_id="site")
        self.service.seal_plan(request_id="seal1", actor_id="op1", plan_id=plan.resource_id)
        _, old_resources = decisions_of(self.service, plan.resource_id)
        self.assertGreater(old_resources[10]["generation_kwh"], 0.0)
        self.service.delay_fuel_batch(request_id="d1", actor_id="op1", batch_id="f2",
                                      new_available_from=iso(T0 + timedelta(hours=20)),
                                      reason="补给船因冰情迟到")
        plans = [item for item in self.service.list_plans("site")
                 if item["status"] == "sealed"]
        self.assertEqual(1, len(plans))
        self.assertEqual("fuel_delay", plans[0]["trigger"])
        _, new_resources = decisions_of(self.service, plans[0]["plan_id"])
        self.assertAlmostEqual(0.0, new_resources[10]["generation_kwh"])
        self.assertGreater(new_resources[20]["generation_kwh"], 0.0)

    def test_resume_continues_unfinished_curtailment_after_restart(self):
        add_fuel(self.service, batch="f2", quantity=0.0)
        self.database.close()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "station.sqlite3"
            database = Database(path)
            clock = MutableClock()
            service = make_service(database, clock)
            add_generator(service, rated=40.0)
            add_fuel(service, quantity=2000.0)
            add_circuit(service, "med", 1, 10.0, state="on")
            add_circuit(service, "comfort", 6, 35.0, state="on")
            plan = service.compute_plan(request_id="p1", actor_id="op1", site_id="site",
                                        horizon_intervals=6, interval_minutes=60)
            service.seal_plan(request_id="seal1", actor_id="op1", plan_id=plan.resource_id)
            database.close()

            reopened = Database(path)
            clock2 = MutableClock(T0 + timedelta(hours=2, minutes=30))
            service2 = EnergyService(reopened, clock2)
            state = service2.get_dispatch_state("site")
            self.assertEqual(plan.resource_id, state["active_plan"]["plan_id"])
            self.assertEqual(iso(T0 + timedelta(hours=2)),
                             state["current_interval"]["interval_start"])
            current = {item["circuit_id"]: item
                       for item in state["current_interval"]["decisions"]}
            self.assertEqual("off", current["comfort"]["decision"])
            self.assertEqual("on", current["med"]["decision"])
            comfort_periods = [item for item in state["curtailments"]
                               if item["circuit_id"] == "comfort"]
            self.assertEqual(1, len(comfort_periods))
            self.assertEqual("active", comfort_periods[0]["status"])
            self.assertEqual(iso(T0 + timedelta(hours=6)), comfort_periods[0]["end"])
            reopened.close()
        self.database = Database()


class SettlementTest(unittest.TestCase):
    """遥测结算幂等，燃料与能量账目闭合。"""

    def setUp(self):
        self.database = Database()
        self.clock = MutableClock()
        self.service = make_service(self.database, self.clock)
        add_generator(self.service, rated=100.0, liters=0.3)
        add_fuel(self.service, quantity=2000.0)
        add_battery(self.service, capacity=200.0, charge=50.0, discharge=50.0,
                    soc=100.0, eff=1.0)
        add_circuit(self.service, "med", 1, 10.0, state="on")

    def tearDown(self):
        self.database.close()

    def _settle(self, request_id, telemetry_id, hour, gen_kwh, med_kwh, soc):
        return self.service.record_telemetry(
            request_id=request_id, actor_id="op1", site_id="site",
            telemetry_id=telemetry_id,
            interval_start=iso(T0 + timedelta(hours=hour)),
            interval_end=iso(T0 + timedelta(hours=hour + 1)),
            generators=[{"generator_id": "g1", "kwh": gen_kwh}],
            battery_soc_kwh=soc,
            circuits=[{"circuit_id": "med", "kwh": med_kwh, "state": "on"}])

    def test_telemetry_replay_does_not_settle_twice(self):
        first = self._settle("t1", "tm-1", 0, 100.0, 95.0, 105.0)
        replay = self._settle("t2", "tm-1", 0, 100.0, 95.0, 105.0)
        self.assertEqual(first.resource_id, replay.resource_id)
        same_request = self._settle("t1", "tm-1", 0, 100.0, 95.0, 105.0)
        self.assertTrue(same_request.replayed)
        account = self.service.verify_energy_account("site")
        self.assertEqual(1, account["settlements"])
        self.assertAlmostEqual(30.0, account["fuel_drawn_liters"])
        with self.assertRaises(ConflictError):
            self._settle("t3", "tm-1", 0, 120.0, 95.0, 105.0)

    def test_energy_and_fuel_accounts_close(self):
        self._settle("t1", "tm-1", 0, 100.0, 95.0, 105.0)
        self._settle("t2", "tm-2", 1, 100.0, 105.0, 100.0)
        account = self.service.verify_energy_account("site")
        self.assertTrue(account["closed"])
        self.assertAlmostEqual(200.0, account["generated_kwh"])
        self.assertAlmostEqual(200.0, account["delivered_kwh"])
        self.assertAlmostEqual(0.0, account["battery_delta_kwh"])
        self.assertAlmostEqual(60.0, account["fuel_settled_liters"])
        self.assertAlmostEqual(60.0, account["fuel_drawn_liters"])
        self.assertAlmostEqual(0.0, account["energy_balance_error_kwh"])
        self.assertAlmostEqual(0.0, account["fuel_balance_error_liters"])

    def test_unbalanced_telemetry_breaks_closure(self):
        self._settle("t1", "tm-1", 0, 100.0, 95.0, 105.0)
        self._settle("t2", "tm-2", 1, 100.0, 50.0, 105.0)
        account = self.service.verify_energy_account("site")
        self.assertFalse(account["closed"])
        self.assertEqual([iso(T0 + timedelta(hours=1))], account["unclosed_intervals"])
        self.assertAlmostEqual(50.0, account["energy_balance_error_kwh"])

    def test_dispatch_state_reports_rationale_and_resources(self):
        add_circuit(self.service, "comfort", 6, 500.0, state="on")
        plan = self.service.compute_plan(request_id="p1", actor_id="op1", site_id="site",
                                         horizon_intervals=4, interval_minutes=60)
        self.service.seal_plan(request_id="seal1", actor_id="op1", plan_id=plan.resource_id)
        state = self.service.get_dispatch_state("site")
        self.assertEqual(plan.resource_id, state["active_plan"]["plan_id"])
        current = {item["circuit_id"]: item
                   for item in state["current_interval"]["decisions"]}
        self.assertEqual("off", current["comfort"]["decision"])
        self.assertEqual("deficit_shed", current["comfort"]["reason_code"])
        self.assertIn("保障等级", current["comfort"]["reason_detail"])
        self.assertEqual("on", current["med"]["decision"])
        self.assertEqual(2000.0, state["fuel"][0]["remaining_liters"])
        rationale = self.service.get_plan_rationale(plan.resource_id)
        comfort = next(item for item in rationale["circuits"]
                       if item["circuit_id"] == "comfort")
        self.assertTrue(all(item["reason_code"] for item in comfort["intervals"]))
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)


class ApiTest(unittest.TestCase):
    """能源接口的 HTTP 边界。"""

    def setUp(self):
        from polar_station_foundation.api import route
        self.route = route
        self.database = Database()
        self.service = make_service(self.database, MutableClock())

    def tearDown(self):
        self.database.close()

    def test_energy_routes(self):
        status, _ = self.route(self.service, "POST", "/energy/generators",
                               {"request_id": "g1", "site_id": "site", "generator_id": "g1",
                                "name": "发电机", "rated_kw": 100.0, "fuel_type": "diesel",
                                "liters_per_kwh": 0.3}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, _ = self.route(self.service, "POST", "/energy/fuel-batches",
                               {"request_id": "f1", "site_id": "site", "batch_id": "f1",
                                "fuel_type": "diesel", "quantity_liters": 2000.0,
                                "available_from": iso(T0)}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, _ = self.route(self.service, "POST", "/energy/circuits",
                               {"request_id": "c1", "site_id": "site", "circuit_id": "med",
                                "name": "医疗", "protection_level": 1, "demand_kw": 10.0,
                                "state": "on"}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, plan = self.route(self.service, "POST", "/energy/plans",
                                  {"request_id": "p1", "site_id": "site",
                                   "horizon_intervals": 4}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, _ = self.route(self.service, "POST", "/energy/plans/seal",
                               {"request_id": "s1", "plan_id": plan["resource_id"]},
                               {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, state = self.route(self.service, "GET",
                                   "/energy/dispatch-state?site_id=site", None)
        self.assertEqual(200, status)
        self.assertEqual(plan["resource_id"], state["active_plan"]["plan_id"])
        status, rationale = self.route(self.service, "GET",
                                       f"/energy/plan-rationale?plan_id={plan['resource_id']}",
                                       None)
        self.assertEqual(200, status)
        self.assertTrue(rationale["circuits"])
        status, account = self.route(self.service, "GET",
                                     "/energy/energy-account?site_id=site", None)
        self.assertEqual(200, status)
        self.assertTrue(account["closed"])
        status, replay = self.route(self.service, "POST", "/energy/plans",
                                    {"request_id": "p1", "site_id": "site",
                                     "horizon_intervals": 4}, {"X-Actor-Id": "op1"})
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])


if __name__ == "__main__":
    unittest.main()
