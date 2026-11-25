import unittest
import time
from src.dispatch_engine import (
    ConnectionStatus,
    DispatchAction,
    FleetControlPlane,
    SiteConfig,
    TelemetryPoint,
)


class TestBatteryFleetDispatchEngine(unittest.TestCase):
    def setUp(self):
        self.control_plane = FleetControlPlane(heartbeat_timeout_seconds=30.0)
        self.site_config = SiteConfig(
            site_id="bess-site-01",
            nominal_energy_kwh=1000.0,
            max_charge_power_kw=500.0,
            max_discharge_power_kw=500.0,
            min_soc_pct=10.0,
            max_soc_pct=90.0,
            max_cell_temp_c=45.0,
        )
        self.control_plane.register_site(self.site_config)

    def test_site_registration_and_initial_state(self):
        site = self.control_plane.get_site("bess-site-01")
        self.assertEqual(site.config.nominal_energy_kwh, 1000.0)
        self.assertEqual(site.status, ConnectionStatus.ONLINE)
        self.assertEqual(site.current_soc_pct, 50.0)

    def test_telemetry_ingestion_updates_state(self):
        t0 = time.time()
        point = TelemetryPoint(
            site_id="bess-site-01",
            current_soc_pct=65.5,
            active_power_kw=250.0,
            cell_temperature_c=28.0,
            timestamp=t0,
        )
        self.control_plane.ingest_telemetry(point)
        site = self.control_plane.get_site("bess-site-01")

        self.assertEqual(site.current_soc_pct, 65.5)
        self.assertEqual(site.active_power_kw, 250.0)
        self.assertEqual(site.cell_temperature_c, 28.0)
        self.assertFalse(site.emergency_lockout)

    def test_thermal_safety_lockout(self):
        point = TelemetryPoint(
            site_id="bess-site-01",
            current_soc_pct=60.0,
            active_power_kw=400.0,
            cell_temperature_c=48.5,  # Exceeds max 45.0C
            timestamp=time.time(),
        )
        self.control_plane.ingest_telemetry(point)
        site = self.control_plane.get_site("bess-site-01")

        self.assertTrue(site.emergency_lockout)
        self.assertIn("exceeds safety limit", site.lockout_reason)

        with self.assertRaises(PermissionError):
            self.control_plane.schedule_dispatch(
                site_id="bess-site-01",
                action=DispatchAction.CHARGE,
                target_power_kw=100.0,
                duration_seconds=300,
                idempotency_token="token-thermal-fail",
            )

    def test_idempotent_command_dispatch(self):
        cmd1 = self.control_plane.schedule_dispatch(
            site_id="bess-site-01",
            action=DispatchAction.CHARGE,
            target_power_kw=200.0,
            duration_seconds=600,
            idempotency_token="idem-tx-001",
        )
        self.assertEqual(cmd1.sequence_number, 1)
        self.assertEqual(cmd1.target_power_kw, 200.0)

        # Retry with identical idempotency token
        cmd2 = self.control_plane.schedule_dispatch(
            site_id="bess-site-01",
            action=DispatchAction.CHARGE,
            target_power_kw=200.0,
            duration_seconds=600,
            idempotency_token="idem-tx-001",
        )
        self.assertEqual(cmd2.sequence_number, 1)
        self.assertEqual(cmd1.command_id, cmd2.command_id)

    def test_charge_soc_upper_limit_rejection(self):
        # Set SOC to 92% (above max_soc_pct 90%)
        self.control_plane.ingest_telemetry(
            TelemetryPoint("bess-site-01", 92.0, 0.0, 24.0, time.time())
        )
        with self.assertRaises(ValueError):
            self.control_plane.schedule_dispatch(
                site_id="bess-site-01",
                action=DispatchAction.CHARGE,
                target_power_kw=150.0,
                duration_seconds=300,
                idempotency_token="token-soc-upper",
            )

    def test_discharge_soc_lower_limit_rejection(self):
        # Set SOC to 8% (below min_soc_pct 10%)
        self.control_plane.ingest_telemetry(
            TelemetryPoint("bess-site-01", 8.0, 0.0, 24.0, time.time())
        )
        with self.assertRaises(ValueError):
            self.control_plane.schedule_dispatch(
                site_id="bess-site-01",
                action=DispatchAction.DISCHARGE,
                target_power_kw=150.0,
                duration_seconds=300,
                idempotency_token="token-soc-lower",
            )

    def test_power_clamping_to_site_ratings(self):
        cmd = self.control_plane.schedule_dispatch(
            site_id="bess-site-01",
            action=DispatchAction.CHARGE,
            target_power_kw=9999.0,  # Max is 500.0
            duration_seconds=300,
            idempotency_token="token-clamp",
        )
        self.assertEqual(cmd.target_power_kw, 500.0)

    def test_heartbeat_timeout_transitions_offline(self):
        now = time.time()
        self.control_plane.ingest_telemetry(
            TelemetryPoint("bess-site-01", 50.0, 100.0, 25.0, timestamp=now)
        )

        # 40 seconds later (timeout is 30.0)
        offline = self.control_plane.evaluate_heartbeats(current_time=now + 40.0)
        self.assertIn("bess-site-01", offline)
        self.assertEqual(self.control_plane.get_site("bess-site-01").status, ConnectionStatus.OFFLINE)

        # Attempting dispatch to offline site raises ConnectionError
        with self.assertRaises(ConnectionError):
            self.control_plane.schedule_dispatch(
                site_id="bess-site-01",
                action=DispatchAction.IDLE,
                target_power_kw=0.0,
                duration_seconds=60,
                idempotency_token="token-offline",
            )


if __name__ == "__main__":
    unittest.main()
