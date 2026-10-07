import unittest

from polar_station_energy.acceptance import run


class EnergyAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertGreaterEqual(result["checks"], 50)
        self.assertGreaterEqual(result["settlements"], 6)


if __name__ == "__main__":
    unittest.main()
