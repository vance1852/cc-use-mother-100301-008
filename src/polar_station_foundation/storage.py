"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_generators (
    generator_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    rated_kw REAL NOT NULL CHECK(rated_kw > 0),
    fuel_type TEXT NOT NULL,
    liters_per_kwh REAL NOT NULL CHECK(liters_per_kwh > 0),
    status TEXT NOT NULL CHECK(status IN ('online', 'offline', 'fault')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_fuel_batches (
    batch_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    fuel_type TEXT NOT NULL,
    quantity_liters REAL NOT NULL CHECK(quantity_liters >= 0),
    remaining_liters REAL NOT NULL CHECK(remaining_liters >= 0),
    available_from TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_batteries (
    battery_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    capacity_kwh REAL NOT NULL CHECK(capacity_kwh > 0),
    max_charge_kw REAL NOT NULL CHECK(max_charge_kw >= 0),
    max_discharge_kw REAL NOT NULL CHECK(max_discharge_kw >= 0),
    soc_kwh REAL NOT NULL CHECK(soc_kwh >= 0),
    efficiency REAL NOT NULL CHECK(efficiency > 0 AND efficiency <= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_circuits (
    circuit_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    protection_level INTEGER NOT NULL CHECK(protection_level BETWEEN 1 AND 9),
    demand_kw REAL NOT NULL CHECK(demand_kw >= 0),
    profile_json TEXT NOT NULL DEFAULT '[]',
    depends_on TEXT REFERENCES energy_circuits(circuit_id),
    group_id TEXT,
    min_run_intervals INTEGER NOT NULL DEFAULT 0 CHECK(min_run_intervals >= 0),
    startup_kwh REAL NOT NULL DEFAULT 0 CHECK(startup_kwh >= 0),
    state TEXT NOT NULL DEFAULT 'off' CHECK(state IN ('on', 'off')),
    on_since TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_experiment_windows (
    window_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    circuit_id TEXT NOT NULL REFERENCES energy_circuits(circuit_id),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    required_kw REAL NOT NULL CHECK(required_kw >= 0),
    approved_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('approved', 'cancelled')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_overrides (
    override_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    circuit_id TEXT NOT NULL REFERENCES energy_circuits(circuit_id),
    action TEXT NOT NULL CHECK(action IN ('force_on', 'force_off')),
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'confirmed', 'expired')),
    requested_by TEXT NOT NULL,
    confirmed_by TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS energy_plans (
    plan_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    horizon_start TEXT NOT NULL,
    horizon_end TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL CHECK(interval_minutes > 0),
    status TEXT NOT NULL CHECK(status IN ('draft', 'sealed', 'superseded')),
    trigger TEXT NOT NULL,
    based_on TEXT,
    alarms_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    sealed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_energy_plans_one_sealed
    ON energy_plans(site_id, horizon_start) WHERE status = 'sealed';
CREATE TABLE IF NOT EXISTS energy_plan_decisions (
    plan_id TEXT NOT NULL REFERENCES energy_plans(plan_id),
    interval_index INTEGER NOT NULL,
    interval_start TEXT NOT NULL,
    circuit_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('on', 'off')),
    reason_code TEXT NOT NULL,
    reason_detail TEXT NOT NULL,
    PRIMARY KEY(plan_id, interval_index, circuit_id)
);
CREATE TABLE IF NOT EXISTS energy_plan_intervals (
    plan_id TEXT NOT NULL REFERENCES energy_plans(plan_id),
    interval_index INTEGER NOT NULL,
    interval_start TEXT NOT NULL,
    demand_kwh REAL NOT NULL,
    generation_kwh REAL NOT NULL,
    charge_kwh REAL NOT NULL,
    discharge_kwh REAL NOT NULL,
    soc_kwh REAL NOT NULL,
    fuel_liters REAL NOT NULL,
    deficit_kwh REAL NOT NULL,
    PRIMARY KEY(plan_id, interval_index)
);
CREATE TABLE IF NOT EXISTS energy_telemetry (
    telemetry_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    interval_start TEXT NOT NULL,
    interval_end TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_settlements (
    settlement_id TEXT PRIMARY KEY,
    telemetry_id TEXT NOT NULL UNIQUE REFERENCES energy_telemetry(telemetry_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    interval_start TEXT NOT NULL,
    generated_kwh REAL NOT NULL,
    delivered_kwh REAL NOT NULL,
    battery_delta_kwh REAL NOT NULL,
    fuel_liters REAL NOT NULL,
    closed INTEGER NOT NULL CHECK(closed IN (0, 1)),
    created_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
