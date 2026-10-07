import unittest
from datetime import datetime, timezone

from polar_station_energy.api import route
from polar_station_energy.clock import ManualClock
from polar_station_energy.service import EnergyService
from polar_station_foundation.storage import Database

T0 = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)
S0 = int(T0.timestamp()) // 1800


class EnergyApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = EnergyService(self.database, ManualClock(T0))
        self.service.register_organization(request_id="req-org", actor_id="bootstrap",
                                           organization_id="o1", name="科考机构")
        self.service.register_actor(request_id="req-admin", actor_id="bootstrap",
                                    new_actor_id="a1", display_name="管理员", role="admin",
                                    organization_id="o1")
        self.service.register_actor(request_id="req-op", actor_id="a1", new_actor_id="op1",
                                    display_name="值班员", role="operator", organization_id="o1")
        self.service.register_site(request_id="req-site", actor_id="a1", site_id="s1",
                                   organization_id="o1", name="科考站", timezone_name="UTC")

    def tearDown(self):
        self.database.close()

    def post(self, path, body, actor="op1"):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor})

    def get(self, path, actor="op1"):
        return route(self.service, "GET", path, None, {"X-Actor-Id": actor})

    def test_health_delegates_to_foundation(self):
        status, payload = self.get("/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_energy_route_returns_404(self):
        status, payload = self.get("/energy/unknown")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_missing_field_returns_400(self):
        status, payload = self.post("/energy/generators", {"request_id": "req-g"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_permission_denied_returns_403(self):
        self.service.register_actor(request_id="req-au", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        status, payload = self.post("/energy/plans", {"request_id": "req-p", "site_id": "s1"},
                                    actor="au1")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_full_plan_flow_over_http(self):
        status, _ = self.post("/energy/generators", {
            "request_id": "req-g", "site_id": "s1", "generator_id": "g1",
            "rated_kw": 30.0, "liters_per_kwh": 0.25})
        self.assertEqual(201, status)
        status, _ = self.post("/energy/fuel-batches", {
            "request_id": "req-f", "site_id": "s1", "batch_id": "f1", "liters": 100.0})
        self.assertEqual(201, status)
        status, _ = self.post("/energy/circuits", {
            "request_id": "req-c1", "site_id": "s1", "circuit_id": "med",
            "name": "医疗", "tier": "medical", "default_kw": 4.0})
        self.assertEqual(201, status)
        status, _ = self.post("/energy/circuits", {
            "request_id": "req-c2", "site_id": "s1", "circuit_id": "work",
            "name": "车间", "tier": "general", "default_kw": 6.0, "shed_rank": 10})
        self.assertEqual(201, status)
        status, payload = self.post("/energy/plans", {
            "request_id": "req-p", "site_id": "s1", "horizon_slots": 4})
        self.assertEqual(201, status)
        plan_id = payload["resource_id"]
        # 重放同一请求返回原方案
        status, replay = self.post("/energy/plans", {
            "request_id": "req-p", "site_id": "s1", "horizon_slots": 4})
        self.assertEqual(200, status)
        self.assertEqual(plan_id, replay["resource_id"])
        self.post("/energy/plans/confirm", {"request_id": "req-pc1", "plan_id": plan_id})
        status, _ = self.post("/energy/plans/confirm", {"request_id": "req-pc2",
                                                        "plan_id": plan_id}, actor="a1")
        self.assertEqual(201, status)
        status, payload = self.get(f"/energy/plan?plan_id={plan_id}")
        self.assertEqual(200, status)
        self.assertEqual("sealed", payload["state"])
        status, payload = self.get(f"/energy/plan-directives?plan_id={plan_id}&slot={S0}")
        self.assertEqual(200, status)
        reasons = {row["circuit_id"]: row["reason_code"] for row in payload["items"]}
        self.assertEqual("tier_must_keep", reasons["med"])
        status, payload = self.get("/energy/overview?site_id=s1")
        self.assertEqual(200, status)
        self.assertEqual(100.0, payload["fuel"]["remaining_liters"])
        status, payload = self.get("/energy/reconcile?site_id=s1")
        self.assertEqual(200, status)
        self.assertTrue(payload["closed"])

    def test_telemetry_endpoint_and_idempotent_replay(self):
        self.post("/energy/generators", {
            "request_id": "req-g", "site_id": "s1", "generator_id": "g1",
            "rated_kw": 30.0, "liters_per_kwh": 0.25})
        self.post("/energy/fuel-batches", {
            "request_id": "req-f", "site_id": "s1", "batch_id": "f1", "liters": 100.0})
        body = {"request_id": "req-t", "site_id": "s1", "slot": S0,
                "generation_kwh": 5.0, "wind_kwh": 0.0, "load_kwh": 5.0,
                "battery_flow_kwh": 0.0}
        status, first = self.post("/energy/telemetry", body)
        self.assertEqual(201, status)
        status, replay = self.post("/energy/telemetry", body)
        self.assertEqual(200, status)
        self.assertEqual(first["resource_id"], replay["resource_id"])
        status, payload = self.get("/energy/settlements?site_id=s1")
        self.assertEqual(1, len(payload["items"]))
        self.assertAlmostEqual(1.25, payload["items"][0]["fuel_liters"])


if __name__ == "__main__":
    unittest.main()
