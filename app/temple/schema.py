from __future__ import annotations

import sqlite3

TEMPLE_SCHEMA = r'''
CREATE TABLE IF NOT EXISTS temple_sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    temple_type TEXT NOT NULL CHECK(temple_type IN ('heritage','urban','mountain','community')),
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','retired')),
    max_concurrent_mitigation_sessions INTEGER NOT NULL CHECK(max_concurrent_mitigation_sessions > 0),
    ventilation_capacity INTEGER NOT NULL CHECK(ventilation_capacity > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS worship_halls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    visit_order INTEGER NOT NULL CHECK(visit_order >= 0),
    expected_visit_seconds INTEGER NOT NULL CHECK(expected_visit_seconds > 0),
    ventilation_capacity INTEGER NOT NULL CHECK(ventilation_capacity > 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closure','disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(temple_id, code),
    UNIQUE(temple_id, visit_order)
);
CREATE TABLE IF NOT EXISTS incense_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incense_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    activity_type TEXT NOT NULL CHECK(activity_type IN ('daily','festival','ceremony','memorial','tour')),
    pm25_target INTEGER NOT NULL CHECK(pm25_target > 0),
    co_target REAL NOT NULL CHECK(co_target >= 0 AND co_target <= 1),
    min_supply_airflow REAL NOT NULL CHECK(min_supply_airflow >= 0),
    min_exhaust_airflow REAL NOT NULL CHECK(min_exhaust_airflow >= 0),
    default_risk_priority INTEGER NOT NULL CHECK(default_risk_priority BETWEEN 0 AND 100),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','retired')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS safety_policy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','published','retired')),
    rules_json TEXT NOT NULL,
    rules_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    published_by TEXT,
    effective_from TEXT,
    retired_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(temple_id, version_no),
    UNIQUE(temple_id, rules_digest)
);
CREATE INDEX IF NOT EXISTS idx_safety_policy_effective ON safety_policy_versions(temple_id,state,effective_from);
CREATE TABLE IF NOT EXISTS incense_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_key TEXT NOT NULL UNIQUE,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    hall_id INTEGER REFERENCES worship_halls(id),
    incense_profile_id INTEGER NOT NULL REFERENCES incense_profiles(id),
    steward_hash TEXT NOT NULL,
    sensor_class TEXT NOT NULL,
    visitor_density REAL NOT NULL CHECK(visitor_density >= 0),
    pm25_ugm3 REAL NOT NULL CHECK(pm25_ugm3 >= 0),
    co_ppm REAL NOT NULL CHECK(co_ppm >= 0 AND co_ppm <= 1),
    supply_airflow REAL NOT NULL CHECK(supply_airflow >= 0),
    exhaust_airflow REAL NOT NULL CHECK(exhaust_airflow >= 0),
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload_digest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observations_scene_time ON incense_observations(temple_id,observed_at);
CREATE TABLE IF NOT EXISTS safety_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL UNIQUE REFERENCES incense_observations(id) ON DELETE CASCADE,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    hall_id INTEGER REFERENCES worship_halls(id),
    incense_profile_id INTEGER NOT NULL REFERENCES incense_profiles(id),
    severity TEXT NOT NULL CHECK(severity IN ('minor','major','critical')),
    reasons_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','mitigating','resolved','expired')),
    opened_at TEXT NOT NULL,
    resolved_at TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_safety_incidents_open ON safety_incidents(state,severity,opened_at);
CREATE TABLE IF NOT EXISTS mitigation_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    safety_incident_id INTEGER NOT NULL REFERENCES safety_incidents(id),
    steward_hash TEXT NOT NULL,
    incense_profile_id INTEGER NOT NULL REFERENCES incense_profiles(id),
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    hall_id INTEGER REFERENCES worship_halls(id),
    safety_policy_version_id INTEGER NOT NULL REFERENCES safety_policy_versions(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','cancelled','expired')),
    allocated_supply_airflow REAL NOT NULL CHECK(allocated_supply_airflow >= 0),
    allocated_exhaust_airflow REAL NOT NULL CHECK(allocated_exhaust_airflow >= 0),
    priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 100),
    started_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(safety_incident_id)
);
CREATE INDEX IF NOT EXISTS idx_mitigation_sessions_ventilation ON mitigation_sessions(temple_id,hall_id,status,expires_at);
CREATE TABLE IF NOT EXISTS ventilation_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mitigation_session_id INTEGER NOT NULL REFERENCES mitigation_sessions(id) ON DELETE CASCADE,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    hall_id INTEGER REFERENCES worship_halls(id),
    supply_airflow REAL NOT NULL,
    exhaust_airflow REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','released')),
    held_at TEXT NOT NULL,
    released_at TEXT,
    UNIQUE(mitigation_session_id)
);
CREATE TABLE IF NOT EXISTS mitigation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mitigation_session_id INTEGER NOT NULL REFERENCES mitigation_sessions(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mitigation_events ON mitigation_events(mitigation_session_id,id);
CREATE TABLE IF NOT EXISTS steward_authorizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    steward_hash TEXT NOT NULL,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    authorization_code TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','expired','cancelled')),
    source_approval_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_authorizations_lookup ON steward_authorizations(steward_hash,temple_id,state,valid_from,valid_until);
CREATE TABLE IF NOT EXISTS restoration_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    strategy TEXT NOT NULL CHECK(strategy IN ('phased','halls','scheduled')),
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','scheduled','running','paused','completed','cancelled')),
    target_percentage INTEGER NOT NULL DEFAULT 100 CHECK(target_percentage BETWEEN 1 AND 100),
    safety_policy_version_id INTEGER NOT NULL REFERENCES safety_policy_versions(id),
    starts_at TEXT,
    ends_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS restoration_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restoration_campaign_id INTEGER NOT NULL REFERENCES restoration_campaigns(id) ON DELETE CASCADE,
    hall_id INTEGER REFERENCES worship_halls(id),
    cohort_key TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','active','paused','completed','failed')),
    activated_at TEXT,
    completed_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(restoration_campaign_id,hall_id,cohort_key)
);
CREATE INDEX IF NOT EXISTS idx_restoration_targets_state ON restoration_targets(restoration_campaign_id,state,id);
CREATE TABLE IF NOT EXISTS hall_closure_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    hall_id INTEGER REFERENCES worship_halls(id),
    code TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'scheduled' CHECK(state IN ('scheduled','active','completed','cancelled')),
    drain_mode TEXT NOT NULL DEFAULT 'finish_active' CHECK(drain_mode IN ('finish_active','cancel_active','block_new')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_closure_active ON hall_closure_windows(temple_id,hall_id,state,starts_at,ends_at);
CREATE TABLE IF NOT EXISTS restoration_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL,
    resource_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_restoration_events_resource ON restoration_events(resource_type,resource_id,id);
CREATE TABLE IF NOT EXISTS restoration_funds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    restriction_type TEXT NOT NULL CHECK(restriction_type IN ('hall','component','unrestricted')),
    hall_id INTEGER REFERENCES worship_halls(id),
    component_code TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','closed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_restoration_funds_temple ON restoration_funds(temple_id,state);
CREATE TABLE IF NOT EXISTS donation_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_id INTEGER NOT NULL REFERENCES restoration_funds(id),
    batch_code TEXT NOT NULL UNIQUE,
    receipt_no TEXT NOT NULL UNIQUE,
    donor_hash TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '',
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    reversed_minor INTEGER NOT NULL DEFAULT 0 CHECK(reversed_minor >= 0),
    received_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'recorded' CHECK(state IN ('recorded','reversed')),
    note TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(reversed_minor <= amount_minor)
);
CREATE INDEX IF NOT EXISTS idx_donation_batches_fund ON donation_batches(fund_id,received_at,id);
CREATE TABLE IF NOT EXISTS budget_commitments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES restoration_campaigns(id),
    fund_id INTEGER NOT NULL REFERENCES restoration_funds(id),
    code TEXT NOT NULL UNIQUE,
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    spent_minor INTEGER NOT NULL DEFAULT 0 CHECK(spent_minor >= 0),
    released_minor INTEGER NOT NULL DEFAULT 0 CHECK(released_minor >= 0),
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','partially_settled','settled','released')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(spent_minor + released_minor <= amount_minor)
);
CREATE INDEX IF NOT EXISTS idx_budget_commitments_campaign ON budget_commitments(campaign_id,state);
CREATE TABLE IF NOT EXISTS fund_ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_id INTEGER NOT NULL REFERENCES restoration_funds(id),
    entry_type TEXT NOT NULL CHECK(entry_type IN ('donation','reversal','commitment','commitment_release','expenditure','transfer_in','transfer_out')),
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    command_key TEXT,
    donation_batch_id INTEGER REFERENCES donation_batches(id),
    commitment_id INTEGER REFERENCES budget_commitments(id),
    counterparty_fund_id INTEGER REFERENCES restoration_funds(id),
    transfer_group_code TEXT,
    linked_entry_id INTEGER REFERENCES fund_ledger_entries(id),
    campaign_id INTEGER REFERENCES restoration_campaigns(id),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fund_ledger_command ON fund_ledger_entries(command_key) WHERE command_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_fund_ledger_fund_time ON fund_ledger_entries(fund_id,created_at,id);
CREATE INDEX IF NOT EXISTS idx_fund_ledger_commitment ON fund_ledger_entries(commitment_id,id);
CREATE TABLE IF NOT EXISTS fund_source_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger_entry_id INTEGER NOT NULL REFERENCES fund_ledger_entries(id) ON DELETE CASCADE,
    donation_batch_id INTEGER REFERENCES donation_batches(id),
    source_entry_id INTEGER REFERENCES fund_ledger_entries(id),
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    CHECK(donation_batch_id IS NOT NULL OR source_entry_id IS NOT NULL),
    UNIQUE(ledger_entry_id, donation_batch_id, source_entry_id)
);
CREATE INDEX IF NOT EXISTS idx_source_allocations_batch ON fund_source_allocations(donation_batch_id);
CREATE INDEX IF NOT EXISTS idx_source_allocations_entry ON fund_source_allocations(source_entry_id);
CREATE TABLE IF NOT EXISTS fund_transfer_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    command_key TEXT NOT NULL UNIQUE,
    from_fund_id INTEGER NOT NULL REFERENCES restoration_funds(id),
    to_fund_id INTEGER NOT NULL REFERENCES restoration_funds(id),
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    reason TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'proposed' CHECK(state IN ('proposed','confirmed','rejected')),
    proposed_by TEXT NOT NULL,
    required_approver TEXT NOT NULL,
    confirmed_by TEXT,
    rejected_by TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    CHECK(from_fund_id <> to_fund_id),
    CHECK(proposed_by <> required_approver)
);
CREATE INDEX IF NOT EXISTS idx_transfer_orders_state ON fund_transfer_orders(state,id);
CREATE VIEW IF NOT EXISTS fund_ledger_movements AS
SELECT id, fund_id, entry_type, amount_minor, command_key, donation_batch_id, commitment_id,
       counterparty_fund_id, transfer_group_code, linked_entry_id, campaign_id, actor, reason, created_at,
       CASE entry_type
           WHEN 'donation' THEN amount_minor
           WHEN 'transfer_in' THEN amount_minor
           WHEN 'reversal' THEN -amount_minor
           WHEN 'expenditure' THEN -amount_minor
           WHEN 'transfer_out' THEN -amount_minor
           ELSE 0
       END AS book_delta,
       CASE entry_type
           WHEN 'commitment' THEN amount_minor
           WHEN 'commitment_release' THEN -amount_minor
           WHEN 'expenditure' THEN -amount_minor
           ELSE 0
       END AS committed_delta
FROM fund_ledger_entries;
'''


def ensure_temple_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(TEMPLE_SCHEMA)
