import unittest
from datetime import datetime, timedelta, timezone

from polar_station_energy.clock import ManualClock
from polar_station_energy.planner import SLOT_MINUTES
from polar_station_energy.service import EnergyService
from polar_station_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from polar_station_foundation.storage import Database

T0 = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)
S0 = int(T0.timestamp()) // (SLOT_MINUTES * 60)


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = ManualClock(T0)
        self.service = EnergyService(self.database, self.clock)
        self.service.register_organization(request_id="req-org", actor_id="bootstrap",
                                           organization_id="o1", name="科考机构")
        self.service.register_actor(request_id="req-admin", actor_id="bootstrap",
                                    new_actor_id="a1", display_name="管理员", role="admin",
                                    organization_id="o1")
        self.service.register_actor(request_id="req-op1", actor_id="a1", new_actor_id="op1",
                                    display_name="值班甲", role="operator", organization_id="o1")
        self.service.register_actor(request_id="req-op2", actor_id="a1", new_actor_id="op2",
                                    display_name="值班乙", role="operator", organization_id="o1")
        self.service.register_actor(request_id="req-au", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_site(request_id="req-site", actor_id="a1", site_id="s1",
                                   organization_id="o1", name="科考站", timezone_name="UTC")

    def tearDown(self):
        self.database.close()

    def add_generator(self, rated=60.0, rate=0.25):
        self.service.register_generator(request_id="req-gen", actor_id="op1", site_id="s1",
                                        generator_id="g1", rated_kw=rated, min_kw=0.0,
                                        liters_per_kwh=rate)

    def add_circuit(self, cid, tier="general", kw=5.0, **kwargs):
        self.service.register_circuit(request_id=f"req-c-{cid}", actor_id="op1", site_id="s1",
                                      circuit_id=cid, name=cid, tier=tier, default_kw=kw, **kwargs)

    def add_basic_station(self):
        self.add_generator()
        self.service.register_battery(request_id="req-bat", actor_id="op1", site_id="s1",
                                      battery_id="b1", capacity_kwh=40.0, max_charge_kw=20.0,
                                      max_discharge_kw=20.0, efficiency=0.95,
                                      initial_soc_kwh=20.0)
        self.service.register_fuel_batch(request_id="req-fuel", actor_id="op1", site_id="s1",
                                         batch_id="f1", liters=400.0, available_slot=S0)
        self.add_circuit("med", "medical", 4.0)
        self.add_circuit("heat", "antifreeze", 6.0)
        self.add_circuit("work", "general", 7.0, shed_rank=30)

    def make_plan(self, request_id, horizon=8):
        return self.service.create_plan(request_id=request_id, actor_id="op1",
                                        site_id="s1", horizon_slots=horizon).resource_id

    def seal(self, plan_id, tag=""):
        self.service.confirm_plan(request_id=f"req-seal{tag}-1", actor_id="op1", plan_id=plan_id)
        self.service.confirm_plan(request_id=f"req-seal{tag}-2", actor_id="op2", plan_id=plan_id)

    def directive(self, plan_id, cid, slot):
        for row in self.service.plan_directives(plan_id, slot):
            if row["circuit_id"] == cid:
                return row
        raise AssertionError(f"缺少指令 {cid}@{slot}")


class PlanLifecycleTest(ServiceTestBase):
    def test_seal_requires_two_distinct_confirmations(self):
        self.add_basic_station()
        plan = self.make_plan("req-p1")
        self.service.confirm_plan(request_id="req-c1", actor_id="op1", plan_id=plan)
        self.assertEqual("draft", self.service.get_plan(plan)["state"])
        with self.assertRaises(ConflictError):
            self.service.confirm_plan(request_id="req-c2", actor_id="op1", plan_id=plan)
        self.service.confirm_plan(request_id="req-c3", actor_id="op2", plan_id=plan)
        self.assertEqual("sealed", self.service.get_plan(plan)["state"])

    def test_confirm_request_replays(self):
        self.add_basic_station()
        plan = self.make_plan("req-p1")
        first = self.service.confirm_plan(request_id="req-c1", actor_id="op1", plan_id=plan)
        replay = self.service.confirm_plan(request_id="req-c1", actor_id="op1", plan_id=plan)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)

    def test_only_newest_plan_can_seal(self):
        self.add_basic_station()
        older = self.make_plan("req-p1")
        newer = self.make_plan("req-p2")
        self.service.confirm_plan(request_id="req-c1", actor_id="op1", plan_id=older)
        with self.assertRaises(ConflictError):
            self.service.confirm_plan(request_id="req-c2", actor_id="op2", plan_id=older)
        self.seal(newer, "n")
        self.assertEqual("sealed", self.service.get_plan(newer)["state"])
        with self.assertRaises(ConflictError):
            self.service.confirm_plan(request_id="req-c3", actor_id="op2", plan_id=older)

    def test_multiple_confirm_requests_seal_only_one_plan(self):
        self.add_basic_station()
        older = self.make_plan("req-p1")
        newer = self.make_plan("req-p2")
        self.seal(newer, "a")
        self.assertEqual("sealed", self.service.get_plan(newer)["state"])
        # 已封存方案再次收到确认请求不会重复封存
        with self.assertRaises(ConflictError):
            self.service.confirm_plan(request_id="req-c9", actor_id="a1", plan_id=newer)
        # 同一轮的旧方案也不能再封存
        with self.assertRaises(ConflictError):
            self.service.confirm_plan(request_id="req-c8", actor_id="op1", plan_id=older)
            self.service.confirm_plan(request_id="req-c7", actor_id="op2", plan_id=older)
        plans = self.service.list_plans("s1")
        self.assertEqual(1, sum(1 for p in plans if p["state"] == "sealed"))

    def test_replan_supersedes_and_keeps_past_directives(self):
        self.add_basic_station()
        plan1 = self.make_plan("req-p1", horizon=8)
        self.seal(plan1, "a")
        before = self.service.plan_directives(plan1)
        self.clock.advance(hours=1)
        plan2 = self.make_plan("req-p2", horizon=8)
        self.seal(plan2, "b")
        self.assertEqual("superseded", self.service.get_plan(plan1)["state"])
        self.assertEqual(before, self.service.plan_directives(plan1))
        self.assertEqual(S0 + 2, self.service.get_plan(plan2)["base_slot"])
        self.assertTrue(all(row["slot"] >= S0 + 2 for row in self.service.plan_directives(plan2)))

    def test_auditor_cannot_plan_or_confirm(self):
        self.add_basic_station()
        with self.assertRaises(PermissionDenied):
            self.service.create_plan(request_id="req-p9", actor_id="au1", site_id="s1")
        plan = self.make_plan("req-p1")
        with self.assertRaises(PermissionDenied):
            self.service.confirm_plan(request_id="req-c9", actor_id="au1", plan_id=plan)


class RegistrationValidationTest(ServiceTestBase):
    def test_unknown_tier_rejected(self):
        with self.assertRaises(ValidationError):
            self.add_circuit("x1", "vip", 1.0)

    def test_must_tier_cannot_join_group(self):
        with self.assertRaises(ValidationError):
            self.add_circuit("med", "medical", 4.0, group_id="g")

    def test_dependency_must_exist(self):
        with self.assertRaises(NotFoundError):
            self.add_circuit("x1", "general", 1.0, depends_on=["ghost"])

    def test_self_dependency_rejected(self):
        with self.assertRaises(ValidationError):
            self.add_circuit("x1", "general", 1.0, depends_on=["x1"])

    def test_window_requires_experiment_tier(self):
        self.add_circuit("lab", "experiment", 5.0)
        self.add_circuit("kitchen", "comfort", 3.0)
        with self.assertRaises(ValidationError):
            self.service.approve_experiment_window(request_id="req-w1", actor_id="op1",
                                                   site_id="s1", window_id="w1",
                                                   circuit_id="kitchen", start_slot=S0,
                                                   end_slot=S0 + 4)
        self.service.approve_experiment_window(request_id="req-w2", actor_id="op1",
                                               site_id="s1", window_id="w2",
                                               circuit_id="lab", start_slot=S0,
                                               end_slot=S0 + 4)

    def test_forecast_for_past_slot_rejected(self):
        self.clock.advance(hours=2)
        with self.assertRaises(ValidationError):
            self.service.set_forecast(request_id="req-f0", actor_id="op1", site_id="s1",
                                      slot=S0, wind_kw=5.0)

    def test_reviewer_cannot_register_assets(self):
        self.service.register_actor(request_id="req-rev", actor_id="a1", new_actor_id="rev1",
                                    display_name="复核员", role="reviewer", organization_id="o1")
        with self.assertRaises(PermissionDenied):
            self.service.register_generator(request_id="req-g9", actor_id="rev1", site_id="s1",
                                            generator_id="g9", rated_kw=10.0)


class TelemetrySettlementTest(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.add_basic_station()
        self.plan = self.make_plan("req-p1", horizon=8)
        self.seal(self.plan, "a")
        self.clock.advance(hours=1)

    def ingest(self, request_id, slot, gen=10.0, wind=0.0, load=10.0, flow=0.0):
        return self.service.ingest_telemetry(
            request_id=request_id, actor_id="op1", site_id="s1", slot=slot,
            generation_kwh=gen, wind_kwh=wind, load_kwh=load, battery_flow_kwh=flow)

    def test_replay_does_not_settle_twice(self):
        first = self.ingest("req-t0", S0)
        replay = self.ingest("req-t0", S0)
        self.assertTrue(replay.replayed)
        self.assertEqual(1, len(self.service.settlements("s1")))
        fuel = self.service.reconcile("s1")["fuel"]
        self.assertAlmostEqual(400.0 - 2.5, fuel["remaining_liters"])
        same = self.ingest("req-t0b", S0)
        self.assertEqual(first.resource_id, same.resource_id)
        self.assertEqual(1, len(self.service.settlements("s1")))

    def test_conflicting_telemetry_rejected(self):
        self.ingest("req-t0", S0)
        with self.assertRaises(ConflictError):
            self.ingest("req-t1", S0, load=11.0)

    def test_out_of_order_settlement_rejected(self):
        self.ingest("req-t1", S0 + 1)
        with self.assertRaises(ConflictError):
            self.ingest("req-t0", S0)

    def test_future_slot_rejected(self):
        with self.assertRaises(ValidationError):
            self.ingest("req-t9", S0 + 100)

    def test_battery_flow_updates_soc(self):
        self.ingest("req-t0", S0, gen=14.0, load=10.0, flow=-4.0)
        overview = self.service.site_overview("s1")
        soc = overview["batteries"][0]["soc_kwh"]
        self.assertAlmostEqual(20.0 + 4.0 * 0.95, soc, places=6)

    def test_discharge_beyond_capability_rejected(self):
        with self.assertRaises(ValidationError):
            self.ingest("req-t0", S0, gen=0.0, load=30.0, flow=30.0)

    def test_reconcile_closes_and_detects_imbalance(self):
        self.ingest("req-t0", S0)
        self.ingest("req-t1", S0 + 1)
        result = self.service.reconcile("s1")
        self.assertTrue(result["closed"])
        self.ingest("req-t2", S0 + 2, gen=5.0, load=9.0)
        result = self.service.reconcile("s1")
        self.assertFalse(result["energy"]["closed"])
        self.assertFalse(result["closed"])
        self.assertEqual([S0 + 2], [v["slot"] for v in result["energy"]["violations"]])

    def test_fuel_ledger_tracks_batches_fifo(self):
        self.service.register_fuel_batch(request_id="req-f2", actor_id="op1", site_id="s1",
                                         batch_id="f2", liters=10.0, available_slot=S0)
        self.ingest("req-t0", S0, gen=20.0, load=20.0)  # 5 L，先扣 f1
        self.ingest("req-t1", S0 + 1, gen=1600.0, load=1600.0)  # 400 L，耗尽 f1 后扣 f2
        fuel = self.service.reconcile("s1")["fuel"]
        self.assertTrue(fuel["closed"])
        self.assertAlmostEqual(410.0 - 405.0, fuel["remaining_liters"])

    def test_delay_consumed_batch_rejected(self):
        self.ingest("req-t0", S0, gen=20.0, load=20.0)
        with self.assertRaises(ConflictError):
            self.service.delay_fuel_batch(request_id="req-d1", actor_id="op1", site_id="s1",
                                          batch_id="f1", available_slot=S0 + 10)


class OverrideTest(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.add_basic_station()
        self.add_circuit("galley", "comfort", 5.0, shed_rank=10)

    def test_override_requires_two_confirmer_identities(self):
        override = self.service.create_override(
            request_id="req-o1", actor_id="op1", site_id="s1", circuit_id="galley",
            slot=S0 + 2, desired_state="on", reason="检修测试", ttl_slots=6).resource_id
        self.service.confirm_override(request_id="req-oc1", actor_id="op1", override_id=override)
        with self.assertRaises(ConflictError):
            self.service.confirm_override(request_id="req-oc2", actor_id="op1", override_id=override)
        self.service.confirm_override(request_id="req-oc3", actor_id="op2", override_id=override)
        plan = self.make_plan("req-p1")
        self.assertEqual("override_forced_on", self.directive(plan, "galley", S0 + 2)["reason_code"])

    def test_override_expires(self):
        override = self.service.create_override(
            request_id="req-o1", actor_id="op1", site_id="s1", circuit_id="galley",
            slot=S0 + 2, desired_state="on", reason="检修测试", ttl_slots=2).resource_id
        self.clock.advance(hours=2)
        with self.assertRaises(ConflictError):
            self.service.confirm_override(request_id="req-oc1", actor_id="op1", override_id=override)

    def test_cannot_override_off_must_tier(self):
        with self.assertRaises(ValidationError):
            self.service.create_override(request_id="req-o2", actor_id="op1", site_id="s1",
                                         circuit_id="med", slot=S0 + 1, desired_state="off",
                                         reason="试图切除医疗", ttl_slots=4)

    def test_cannot_override_past_slot(self):
        self.clock.advance(hours=1)
        with self.assertRaises(ValidationError):
            self.service.create_override(request_id="req-o3", actor_id="op1", site_id="s1",
                                         circuit_id="galley", slot=S0, desired_state="on",
                                         reason="过去时段", ttl_slots=4)


class FailureAndResumeTest(ServiceTestBase):
    def test_failure_triggers_future_only_replan(self):
        self.add_basic_station()
        plan1 = self.make_plan("req-p1", horizon=8)
        self.seal(plan1, "a")
        self.clock.advance(hours=1)
        self.service.report_failure(request_id="req-fail", actor_id="op1", site_id="s1",
                                    kind="generator", asset_id="g1")
        plan2 = self.make_plan("req-p2", horizon=8)
        self.seal(plan2, "b")
        self.assertTrue(self.service.get_plan(plan2)["breached"])
        self.assertEqual("on", self.directive(plan1, "work", S0)["state"])
        # 储能在两个时段后耗尽，一般负荷从第三时段起被功率缺口切除
        self.assertEqual("kept_within_budget", self.directive(plan2, "work", S0 + 2)["reason_code"])
        self.assertEqual("shed_deficit", self.directive(plan2, "work", S0 + 4)["reason_code"])
        self.assertEqual(8 * 3, len(self.service.plan_directives(plan1)))
        with self.assertRaises(ConflictError):
            self.service.report_failure(request_id="req-fail2", actor_id="op1", site_id="s1",
                                        kind="generator", asset_id="g1")
        self.service.clear_failure(request_id="req-fix", actor_id="op1", site_id="s1",
                                   kind="generator", asset_id="g1")
        plan3 = self.make_plan("req-p3", horizon=8)
        self.assertFalse(self.service.get_plan(plan3)["breached"])

    def test_min_run_carries_across_replans(self):
        self.add_basic_station()
        self.service.register_circuit(request_id="req-c-pump", actor_id="op1", site_id="s1",
                                      circuit_id="pump", name="泵", tier="general",
                                      default_kw=5.0, min_run_slots=4, shed_rank=80)
        plan1 = self.make_plan("req-p1", horizon=8)
        self.seal(plan1, "a")
        self.assertEqual("on", self.directive(plan1, "pump", S0)["state"])
        self.clock.advance(minutes=30)
        self.service.report_failure(request_id="req-fail", actor_id="op1", site_id="s1",
                                    kind="generator", asset_id="g1")
        plan2 = self.make_plan("req-p2", horizon=8)
        # 发电机故障、储能有限，但最低运行时长仍守住已投入设备
        self.assertEqual("min_run_active", self.directive(plan2, "pump", S0 + 1)["reason_code"])
        self.assertEqual("min_run_active", self.directive(plan2, "pump", S0 + 3)["reason_code"])

    def test_resume_after_restart_continues_open_episode(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "energy.sqlite3"
            database = Database(path)
            clock = ManualClock(T0)
            service = EnergyService(database, clock)
            service.register_organization(request_id="req-org", actor_id="bootstrap",
                                          organization_id="o1", name="科考机构")
            service.register_actor(request_id="req-admin", actor_id="bootstrap",
                                   new_actor_id="a1", display_name="管理员", role="admin",
                                   organization_id="o1")
            service.register_actor(request_id="req-op1", actor_id="a1", new_actor_id="op1",
                                   display_name="值班甲", role="operator", organization_id="o1")
            service.register_actor(request_id="req-op2", actor_id="a1", new_actor_id="op2",
                                   display_name="值班乙", role="operator", organization_id="o1")
            service.register_site(request_id="req-site", actor_id="a1", site_id="s1",
                                  organization_id="o1", name="科考站", timezone_name="UTC")
            service.register_generator(request_id="req-g", actor_id="op1", site_id="s1",
                                       generator_id="g1", rated_kw=10.0, liters_per_kwh=0.5)
            service.register_fuel_batch(request_id="req-f", actor_id="op1", site_id="s1",
                                        batch_id="f1", liters=8.0, available_slot=S0)
            service.register_circuit(request_id="req-cm", actor_id="op1", site_id="s1",
                                     circuit_id="med", name="医疗", tier="medical", default_kw=4.0)
            service.register_circuit(request_id="req-cw", actor_id="op1", site_id="s1",
                                     circuit_id="work", name="车间", tier="general",
                                     default_kw=6.0, shed_rank=10)
            plan1 = service.create_plan(request_id="req-p1", actor_id="op1",
                                        site_id="s1", horizon_slots=4).resource_id
            service.confirm_plan(request_id="req-c1", actor_id="op1", plan_id=plan1)
            service.confirm_plan(request_id="req-c2", actor_id="op2", plan_id=plan1)
            episode = service.curtailment_status("s1")["open_episode"]
            self.assertIsNotNone(episode)
            database.close()

            # 服务恢复运行：原方案已过期，限电周期仍未结束
            clock.set(T0 + timedelta(hours=4))
            reopened = Database(path)
            service2 = EnergyService(reopened, clock)
            status = service2.curtailment_status("s1")
            self.assertEqual(episode["episode_id"], status["open_episode"]["episode_id"])
            receipt = service2.resume_site(request_id="req-resume", actor_id="op1", site_id="s1",
                                           horizon_slots=4)
            plan2 = receipt.resource_id
            self.assertEqual(S0 + 8, service2.get_plan(plan2)["base_slot"])
            service2.confirm_plan(request_id="req-c3", actor_id="op1", plan_id=plan2)
            service2.confirm_plan(request_id="req-c4", actor_id="op2", plan_id=plan2)
            # 燃料早已耗尽，限电周期继续推进而不是重新开始
            status = service2.curtailment_status("s1")
            self.assertEqual(episode["episode_id"], status["open_episode"]["episode_id"])
            self.assertTrue(any(a["action"] == "shed" for a in status["actions"]))
            valid, _ = service2.verify_audit()
            self.assertTrue(valid)
            reopened.close()


class EpisodeLifecycleTest(ServiceTestBase):
    def test_episode_opens_and_closes_with_recovery(self):
        self.add_generator(rated=10.0, rate=0.5)
        self.service.register_fuel_batch(request_id="req-fuel", actor_id="op1", site_id="s1",
                                         batch_id="f1", liters=10.0, available_slot=S0)
        self.add_circuit("med", "medical", 4.0)
        self.add_circuit("work", "general", 6.0, shed_rank=10)
        plan1 = self.make_plan("req-p1", horizon=8)
        self.seal(plan1, "a")
        status = self.service.curtailment_status("s1")
        self.assertIsNotNone(status["open_episode"])
        self.assertTrue(any(a["reason_code"] == "shed_fuel_reserve" for a in status["actions"]))
        # 燃料补给到达并恢复充足，重新规划后限电周期关闭
        self.service.register_fuel_batch(request_id="req-f2", actor_id="op1", site_id="s1",
                                         batch_id="f2", liters=500.0, available_slot=S0)
        plan2 = self.make_plan("req-p2", horizon=8)
        self.seal(plan2, "b")
        status = self.service.curtailment_status("s1")
        self.assertIsNone(status["open_episode"])
        self.assertEqual(1, status["closed_episodes"])
        self.assertTrue(any(a["action"] == "restore" for a in status["actions"]))


if __name__ == "__main__":
    unittest.main()
