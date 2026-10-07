import unittest

from polar_station_energy.planner import (
    REASON_ASSET_FAILED,
    REASON_DEPENDENCY,
    REASON_KEPT,
    REASON_MIN_RUN,
    REASON_OUTSIDE_WINDOW,
    REASON_OVERRIDE_OFF,
    REASON_OVERRIDE_ON,
    REASON_SHED_DEFICIT,
    REASON_SHED_FUEL,
    REASON_SHED_GROUP,
    REASON_TIER_MUST,
    REASON_WINDOW,
    BatterySpec,
    CircuitSpec,
    FuelBatchSpec,
    GeneratorSpec,
    PlanInput,
    run_plan,
)

SLOT_HOURS = 0.5


def circuit(cid, tier="general", kw=5.0, rank=0, group=None, deps=(), run=0, startup=0.0,
            failed=None, overrides=None):
    return CircuitSpec(
        circuit_id=cid, tier=tier, default_kw=kw, demand_overrides=overrides or {},
        group_id=group, depends_on=tuple(deps), min_run_slots=run, startup_kwh=startup,
        shed_rank=rank, failed_from_slot=failed)


def plan_input(start=0, horizon=4, circuits=(), generators=(), batteries=(), fuel=(),
               wind=None, windows=None, overrides=None, prior_on=None, on_since=None):
    return PlanInput(
        start_slot=start, horizon=horizon, slot_hours=SLOT_HOURS,
        circuits=tuple(circuits), generators=tuple(generators), batteries=tuple(batteries),
        fuel_batches=tuple(fuel), wind_kw=wind or {}, windows=windows or {},
        overrides=overrides or {}, prior_on=prior_on or {}, on_since=on_since or {})


def directive_at(output, cid, slot):
    for d in output.directives:
        if d.circuit_id == cid and d.slot == slot:
            return d
    raise AssertionError(f"缺少指令 {cid}@{slot}")


def dispatch_at(output, slot):
    for d in output.dispatch:
        if d.slot == slot:
            return d
    raise AssertionError(f"缺少调度 {slot}")


class MustKeepTest(unittest.TestCase):
    def test_must_tiers_survive_until_battery_empty(self):
        out = run_plan(plan_input(
            horizon=4,
            circuits=[
                circuit("med", "medical", 4.0),
                circuit("comm", "comms", 2.0),
                circuit("heat", "antifreeze", 6.0),
                circuit("life", "life_support", 8.0, rank=90),
            ],
            generators=[GeneratorSpec("g1", 50.0, 0.0, 0.25, failed_from_slot=0)],
            batteries=[BatterySpec("b1", 12.0, 24.0, 24.0, 1.0, 12.0)],
        ))
        # 没有燃料时一般负荷立即为燃料储备切除
        self.assertEqual(REASON_SHED_FUEL, directive_at(out, "life", 0).reason)
        for slot in range(4):
            for cid in ("med", "comm", "heat"):
                self.assertEqual(REASON_TIER_MUST, directive_at(out, cid, slot).reason)
        # 12 kWh 储能以 12 kW 供电可维持两个 0.5h 时段，第三时段失守
        self.assertFalse(dispatch_at(out, 0).breach)
        self.assertFalse(dispatch_at(out, 1).breach)
        self.assertTrue(dispatch_at(out, 2).breach)
        self.assertAlmostEqual(12.0, dispatch_at(out, 2).unserved_kw)
        self.assertTrue(out.breached)

    def test_failed_must_circuit_marks_breach(self):
        out = run_plan(plan_input(
            horizon=2,
            circuits=[circuit("heat", "antifreeze", 6.0, failed=0)],
            generators=[GeneratorSpec("g1", 50.0, 0.0, 0.25)],
            fuel=[FuelBatchSpec("f1", 100.0, 0)],
        ))
        self.assertEqual(REASON_ASSET_FAILED, directive_at(out, "heat", 0).reason)
        self.assertTrue(dispatch_at(out, 0).breach)


class ShedOrderTest(unittest.TestCase):
    def circuits(self):
        return [
            circuit("med", "medical", 4.0),
            circuit("heat", "antifreeze", 6.0),
            circuit("life", "life_support", 8.0, rank=90),
            circuit("green", "general", 6.0, rank=40, deps=("work",)),
            circuit("work", "general", 7.0, rank=30),
            circuit("galley", "comfort", 5.0, rank=20, group="hab"),
            circuit("laundry", "comfort", 3.0, rank=10, group="hab"),
        ]

    def run_with_gen(self, rated):
        return run_plan(plan_input(
            horizon=1, circuits=self.circuits(),
            generators=[GeneratorSpec("g1", rated, 0.0, 0.25)],
            fuel=[FuelBatchSpec("f1", 1000.0, 0)],
        ))

    def test_lower_rank_shed_first_and_group_moves_together(self):
        out = self.run_with_gen(20.0)
        self.assertEqual(REASON_KEPT, directive_at(out, "life", 0).reason)
        for cid in ("green", "work", "galley", "laundry"):
            self.assertEqual(REASON_SHED_DEFICIT, directive_at(out, cid, 0).reason)
        # 成组设备同一时段以相同依据整组切除
        self.assertEqual(directive_at(out, "galley", 0).reason,
                         directive_at(out, "laundry", 0).reason)

    def test_dependency_blocks_dependent_only(self):
        out = self.run_with_gen(27.0)
        self.assertTrue(directive_at(out, "work", 0).on)
        self.assertFalse(directive_at(out, "green", 0).on)
        self.assertEqual(REASON_SHED_DEFICIT, directive_at(out, "green", 0).reason)

    def test_dependent_pulls_in_its_dependency(self):
        out = self.run_with_gen(40.0)
        self.assertTrue(directive_at(out, "green", 0).on)
        self.assertEqual(REASON_DEPENDENCY, directive_at(out, "work", 0).reason)

    def test_group_member_failure_sheds_whole_group(self):
        circuits = self.circuits()
        circuits[6] = circuit("laundry", "comfort", 3.0, rank=10, group="hab", failed=0)
        out = run_plan(plan_input(
            horizon=1, circuits=circuits,
            generators=[GeneratorSpec("g1", 100.0, 0.0, 0.25)],
            fuel=[FuelBatchSpec("f1", 1000.0, 0)],
        ))
        self.assertEqual(REASON_ASSET_FAILED, directive_at(out, "laundry", 0).reason)
        self.assertEqual(REASON_SHED_GROUP, directive_at(out, "galley", 0).reason)


class MinRunAndStartupTest(unittest.TestCase):
    def test_min_run_forces_on_even_without_supply(self):
        out = run_plan(plan_input(
            start=2, horizon=3,
            circuits=[circuit("work", "general", 7.0, rank=30, run=2)],
            generators=[GeneratorSpec("g1", 50.0, 0.0, 0.25, failed_from_slot=0)],
            prior_on={"work": True}, on_since={"work": 1},
        ))
        self.assertEqual(REASON_MIN_RUN, directive_at(out, "work", 2).reason)
        self.assertTrue(dispatch_at(out, 2).breach)
        self.assertFalse(directive_at(out, "work", 3).on)

    def test_startup_cost_counts_once(self):
        out = run_plan(plan_input(
            horizon=2,
            circuits=[circuit("med", "medical", 10.0),
                      circuit("work", "general", 7.0, rank=30, startup=1.0)],
            generators=[GeneratorSpec("g1", 20.0, 0.0, 0.25)],
            fuel=[FuelBatchSpec("f1", 1000.0, 0)],
        ))
        self.assertAlmostEqual(19.0, dispatch_at(out, 0).demand_kw)
        self.assertAlmostEqual(17.0, dispatch_at(out, 1).demand_kw)
        self.assertIn("启动代价", directive_at(out, "work", 0).detail)

    def test_startup_cost_can_block_acceptance(self):
        out = run_plan(plan_input(
            horizon=1,
            circuits=[circuit("med", "medical", 10.0),
                      circuit("work", "general", 7.0, rank=30, startup=2.0)],
            generators=[GeneratorSpec("g1", 18.0, 0.0, 0.25)],
            fuel=[FuelBatchSpec("f1", 1000.0, 0)],
        ))
        # 7 kW 本身可以承担，叠加 4 kW 启动等效后越限
        self.assertEqual(REASON_SHED_DEFICIT, directive_at(out, "work", 0).reason)


class WindowAndOverrideTest(unittest.TestCase):
    def test_experiment_only_runs_inside_window(self):
        out = run_plan(plan_input(
            horizon=6,
            circuits=[circuit("lab", "experiment", 10.0, rank=50)],
            generators=[GeneratorSpec("g1", 100.0, 0.0, 0.25)],
            fuel=[FuelBatchSpec("f1", 1000.0, 0)],
            windows={"lab": ((2, 4),)},
        ))
        for slot in (0, 1, 4, 5):
            self.assertEqual(REASON_OUTSIDE_WINDOW, directive_at(out, "lab", slot).reason)
        for slot in (2, 3):
            self.assertEqual(REASON_WINDOW, directive_at(out, "lab", slot).reason)

    def test_override_on_and_off(self):
        out = run_plan(plan_input(
            horizon=2,
            circuits=[circuit("med", "medical", 10.0),
                      circuit("life", "life_support", 8.0, rank=90),
                      circuit("work", "general", 7.0, rank=30)],
            generators=[GeneratorSpec("g1", 25.0, 0.0, 0.25)],
            fuel=[FuelBatchSpec("f1", 1000.0, 0)],
            overrides={("work", 0): "on", ("life", 1): "off"},
        ))
        self.assertEqual(REASON_OVERRIDE_ON, directive_at(out, "work", 0).reason)
        self.assertEqual(REASON_KEPT, directive_at(out, "life", 0).reason)
        self.assertEqual(REASON_OVERRIDE_OFF, directive_at(out, "life", 1).reason)

    def test_override_on_consumes_deficit_budget(self):
        out = run_plan(plan_input(
            horizon=1,
            circuits=[circuit("med", "medical", 10.0),
                      circuit("life", "life_support", 8.0, rank=90),
                      circuit("work", "general", 7.0, rank=30)],
            generators=[GeneratorSpec("g1", 17.0, 0.0, 0.25)],
            fuel=[FuelBatchSpec("f1", 1000.0, 0)],
            overrides={("work", 0): "on"},
        ))
        # 覆盖强制车间供电后，生活保障让位于功率缺口
        self.assertEqual(REASON_OVERRIDE_ON, directive_at(out, "work", 0).reason)
        self.assertEqual(REASON_SHED_DEFICIT, directive_at(out, "life", 0).reason)


class FuelPacingTest(unittest.TestCase):
    def test_optional_load_paces_against_batch_arrival(self):
        out = run_plan(plan_input(
            horizon=6,
            circuits=[circuit("med", "medical", 4.0),
                      circuit("opt", "general", 8.0, rank=10)],
            generators=[GeneratorSpec("g1", 100.0, 0.0, 0.5)],
            fuel=[FuelBatchSpec("f1", 8.0, 0), FuelBatchSpec("f2", 10.0, 3)],
        ))
        # 第一批 8 L = 16 kWh：保障 2 kWh/时段，一般负荷前两时段各 4 kWh 可承担
        self.assertTrue(directive_at(out, "opt", 0).on)
        self.assertTrue(directive_at(out, "opt", 1).on)
        # 第三时段继续供电将透支保障储备，切除等待第二批燃料
        self.assertEqual(REASON_SHED_FUEL, directive_at(out, "opt", 2).reason)
        self.assertAlmostEqual(1.0, dispatch_at(out, 2).fuel_liters)
        # 第二批到达后恢复
        self.assertTrue(directive_at(out, "opt", 3).on)
        for slot in range(6):
            self.assertTrue(directive_at(out, "med", slot).on)
            self.assertFalse(dispatch_at(out, slot).breach)

    def test_demand_overrides_shift_consumption(self):
        out = run_plan(plan_input(
            horizon=2,
            circuits=[circuit("med", "medical", 4.0),
                      circuit("galley", "comfort", 2.0, rank=10, overrides={0: 8.0})],
            generators=[GeneratorSpec("g1", 10.0, 0.0, 0.25)],
            fuel=[FuelBatchSpec("f1", 1000.0, 0)],
        ))
        # 分时需求 8 kW 超出供给被切除，平时段 2 kW 保留
        self.assertEqual(REASON_SHED_DEFICIT, directive_at(out, "galley", 0).reason)
        self.assertEqual(REASON_KEPT, directive_at(out, "galley", 1).reason)


if __name__ == "__main__":
    unittest.main()
