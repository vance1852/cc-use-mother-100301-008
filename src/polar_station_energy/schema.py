"""能源域的 SQLite 表结构，与基础服务共用同一数据库连接。"""

ENERGY_SCHEMA = """
CREATE TABLE IF NOT EXISTS energy_generators (
    generator_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    rated_kw REAL NOT NULL CHECK(rated_kw > 0),
    min_kw REAL NOT NULL DEFAULT 0 CHECK(min_kw >= 0),
    liters_per_kwh REAL NOT NULL CHECK(liters_per_kwh > 0),
    failed_from_slot INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_batteries (
    battery_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    capacity_kwh REAL NOT NULL CHECK(capacity_kwh > 0),
    max_charge_kw REAL NOT NULL CHECK(max_charge_kw >= 0),
    max_discharge_kw REAL NOT NULL CHECK(max_discharge_kw >= 0),
    efficiency REAL NOT NULL CHECK(efficiency > 0 AND efficiency <= 1),
    initial_soc_kwh REAL NOT NULL CHECK(initial_soc_kwh >= 0),
    failed_from_slot INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_wind_turbines (
    wind_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    rated_kw REAL NOT NULL CHECK(rated_kw > 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_fuel_batches (
    batch_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    liters REAL NOT NULL CHECK(liters >= 0),
    remaining_liters REAL NOT NULL CHECK(remaining_liters >= 0),
    available_slot INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_circuits (
    circuit_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    tier TEXT NOT NULL CHECK(tier IN ('medical','comms','antifreeze','life_support','experiment','comfort','general')),
    default_kw REAL NOT NULL CHECK(default_kw >= 0),
    demand_overrides_json TEXT NOT NULL DEFAULT '{}',
    group_id TEXT,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    min_run_slots INTEGER NOT NULL DEFAULT 0 CHECK(min_run_slots >= 0),
    startup_kwh REAL NOT NULL DEFAULT 0 CHECK(startup_kwh >= 0),
    shed_rank INTEGER NOT NULL DEFAULT 0,
    failed_from_slot INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_experiment_windows (
    window_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    circuit_id TEXT NOT NULL REFERENCES energy_circuits(circuit_id),
    start_slot INTEGER NOT NULL,
    end_slot INTEGER NOT NULL CHECK(end_slot > start_slot),
    approved_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_forecasts (
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    slot INTEGER NOT NULL,
    wind_kw REAL NOT NULL CHECK(wind_kw >= 0),
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(site_id, slot)
);
CREATE TABLE IF NOT EXISTS energy_plans (
    plan_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    base_slot INTEGER NOT NULL,
    horizon_slots INTEGER NOT NULL CHECK(horizon_slots > 0),
    state TEXT NOT NULL CHECK(state IN ('draft','sealed','superseded')),
    breached INTEGER NOT NULL DEFAULT 0,
    episode_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sealed_at TEXT
);
CREATE TABLE IF NOT EXISTS energy_plan_confirmations (
    plan_id TEXT NOT NULL REFERENCES energy_plans(plan_id),
    actor_id TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, actor_id)
);
CREATE TABLE IF NOT EXISTS energy_plan_directives (
    plan_id TEXT NOT NULL REFERENCES energy_plans(plan_id),
    circuit_id TEXT NOT NULL,
    slot INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('on','off')),
    reason_code TEXT NOT NULL,
    reason_detail TEXT NOT NULL,
    PRIMARY KEY(plan_id, circuit_id, slot)
);
CREATE TABLE IF NOT EXISTS energy_plan_dispatch (
    plan_id TEXT NOT NULL REFERENCES energy_plans(plan_id),
    slot INTEGER NOT NULL,
    demand_kw REAL NOT NULL,
    supply_kw REAL NOT NULL,
    gen_kw REAL NOT NULL,
    wind_kw REAL NOT NULL,
    battery_kw REAL NOT NULL,
    soc_kwh REAL NOT NULL,
    fuel_liters REAL NOT NULL,
    unserved_kw REAL NOT NULL,
    breach INTEGER NOT NULL CHECK(breach IN (0, 1)),
    PRIMARY KEY(plan_id, slot)
);
CREATE TABLE IF NOT EXISTS energy_overrides (
    override_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    circuit_id TEXT NOT NULL REFERENCES energy_circuits(circuit_id),
    slot INTEGER NOT NULL,
    desired_state TEXT NOT NULL CHECK(desired_state IN ('on','off')),
    reason TEXT NOT NULL,
    expires_slot INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','active','expired')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    activated_at TEXT
);
CREATE TABLE IF NOT EXISTS energy_override_confirmations (
    override_id TEXT NOT NULL REFERENCES energy_overrides(override_id),
    actor_id TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(override_id, actor_id)
);
CREATE TABLE IF NOT EXISTS energy_telemetry (
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    slot INTEGER NOT NULL,
    generation_kwh REAL NOT NULL,
    wind_kwh REAL NOT NULL,
    load_kwh REAL NOT NULL,
    battery_flow_kwh REAL NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(site_id, slot)
);
CREATE TABLE IF NOT EXISTS energy_settlements (
    settlement_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    slot INTEGER NOT NULL,
    generation_kwh REAL NOT NULL,
    wind_kwh REAL NOT NULL,
    load_kwh REAL NOT NULL,
    battery_flow_kwh REAL NOT NULL,
    planned_kwh REAL NOT NULL,
    fuel_liters REAL NOT NULL,
    unserved_kwh REAL NOT NULL,
    imbalance_kwh REAL NOT NULL,
    soc_kwh REAL NOT NULL,
    fuel_short INTEGER NOT NULL DEFAULT 0 CHECK(fuel_short IN (0, 1)),
    settled_by TEXT NOT NULL,
    settled_at TEXT NOT NULL,
    UNIQUE(site_id, slot)
);
CREATE TABLE IF NOT EXISTS energy_fuel_ledger (
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    slot INTEGER NOT NULL,
    batch_id TEXT NOT NULL REFERENCES energy_fuel_batches(batch_id),
    liters REAL NOT NULL,
    PRIMARY KEY(site_id, slot, batch_id)
);
CREATE TABLE IF NOT EXISTS energy_battery_soc (
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    battery_id TEXT NOT NULL REFERENCES energy_batteries(battery_id),
    slot INTEGER NOT NULL,
    soc_kwh REAL NOT NULL,
    PRIMARY KEY(site_id, battery_id, slot)
);
CREATE TABLE IF NOT EXISTS energy_episodes (
    episode_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    started_slot INTEGER NOT NULL,
    ended_slot INTEGER,
    status TEXT NOT NULL CHECK(status IN ('open','closed')),
    cause TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS energy_episode_actions (
    episode_id TEXT NOT NULL REFERENCES energy_episodes(episode_id),
    circuit_id TEXT NOT NULL,
    slot INTEGER NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('shed','restore','breach')),
    reason_code TEXT NOT NULL,
    PRIMARY KEY(episode_id, circuit_id, slot, action)
);
"""


def ensure_energy_schema(connection) -> None:
    """在既有数据库连接上建立能源域表结构（幂等）。"""

    connection.executescript(ENERGY_SCHEMA)
