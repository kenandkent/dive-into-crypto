-- Short-Lab H01 schema (design B28, migrations/005_hedge_advisor.sql).
--
-- Forward-only, idempotent: every statement uses IF NOT EXISTS and
-- `sl_schema_version` guarantees each migration file applies once.
-- 001-004 are frozen and must never be modified; new Hedge tables land
-- here (005); later structures use 006+.
-- One DDL transaction per file: all tables, indexes and the version row
-- commit together via repository._migrate_sync; any failure rolls back to
-- schema version 4 and Hedge stays unavailable while 001-004 keep serving.
-- Application maintains `sl_hedge_snapshot_reference` integrity in the same
-- single-worker transaction (no DB-level FK); DOUBLE columns in
-- sl_hedge_plan/sl_hedge_leg are display/sort projections only and must
-- never be read back for ledger balances (B28.0).

CREATE TABLE IF NOT EXISTS sl_hedge_simulation_snapshot (
  simulation_id VARCHAR PRIMARY KEY,
  symbol VARCHAR NOT NULL,
  generated_at_ms BIGINT NOT NULL,
  expires_at_ms BIGINT NOT NULL,
  formula_version VARCHAR NOT NULL,
  policy_hash VARCHAR NOT NULL,
  input_json VARCHAR NOT NULL,
  result_json VARCHAR NOT NULL,
  source_meta_json VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS sl_hedge_venue_mapping (
  mapping_id VARCHAR PRIMARY KEY,
  canonical_id VARCHAR NOT NULL,
  venue VARCHAR NOT NULL,
  instrument_id VARCHAR NOT NULL,
  mapping_version VARCHAR NOT NULL,
  verification_json VARCHAR NOT NULL,
  verified_at_ms BIGINT NOT NULL,
  UNIQUE(canonical_id, venue, instrument_id, mapping_version)
);
CREATE TABLE IF NOT EXISTS sl_hedge_fill_event (
  event_id VARCHAR PRIMARY KEY,
  plan_id VARCHAR NOT NULL,
  client_event_id VARCHAR NOT NULL,
  leg_type VARCHAR NOT NULL,
  event_type VARCHAR NOT NULL,
  executed_at_ms BIGINT NOT NULL,
  recorded_at_ms BIGINT NOT NULL,
  event_json VARCHAR NOT NULL,
  UNIQUE(plan_id, client_event_id)
);
CREATE TABLE IF NOT EXISTS sl_hedge_snapshot_reference (
  referrer_type VARCHAR NOT NULL,
  referrer_id VARCHAR NOT NULL,
  referenced_type VARCHAR NOT NULL,
  referenced_id VARCHAR NOT NULL,
  purpose VARCHAR NOT NULL,
  created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(referrer_type, referrer_id, referenced_type, referenced_id, purpose)
);
CREATE INDEX IF NOT EXISTS idx_sl_hedge_reference_target
ON sl_hedge_snapshot_reference(referenced_type, referenced_id);
CREATE INDEX IF NOT EXISTS idx_sl_hedge_fill_time
ON sl_hedge_fill_event(plan_id, executed_at_ms, event_id);
CREATE TABLE IF NOT EXISTS sl_hedge_outcome (
  outcome_id VARCHAR PRIMARY KEY,
  fcs_snapshot_id VARCHAR NOT NULL,
  strategy VARCHAR NOT NULL,
  horizon_days INTEGER NOT NULL,
  outcome_status VARCHAR NOT NULL,
  reason_code VARCHAR,
  evidence_version VARCHAR NOT NULL,
  cost_config_hash VARCHAR NOT NULL,
  outcome_json VARCHAR NOT NULL,
  updated_at_ms BIGINT NOT NULL,
  UNIQUE(fcs_snapshot_id, strategy, horizon_days, evidence_version, cost_config_hash)
);
CREATE TABLE IF NOT EXISTS sl_funding_capture_snapshot (
  snapshot_id VARCHAR PRIMARY KEY,
  symbol VARCHAR NOT NULL,
  canonical_id VARCHAR NOT NULL,
  as_of_ms BIGINT NOT NULL,
  fcs_version VARCHAR NOT NULL,
  fcs_config_hash VARCHAR NOT NULL,
  reference_notional_usd DOUBLE NOT NULL,
  fcs DOUBLE,
  module_scores_json VARCHAR NOT NULL,
  funding_metrics_json VARCHAR NOT NULL,
  venue_summary_json VARCHAR NOT NULL,
  basis_json VARCHAR,
  risk_json VARCHAR NOT NULL,
  readiness VARCHAR NOT NULL,
  reasons_json VARCHAR NOT NULL,
  created_at_ms BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sl_fcs_symbol_time
ON sl_funding_capture_snapshot(symbol, as_of_ms);
CREATE TABLE IF NOT EXISTS sl_spot_venue_snapshot (
  snapshot_id VARCHAR PRIMARY KEY,
  canonical_id VARCHAR NOT NULL,
  venue VARCHAR NOT NULL,
  venue_symbol VARCHAR,
  chain VARCHAR,
  contract_address VARCHAR,
  as_of_ms BIGINT NOT NULL,
  fetched_at_ms BIGINT NOT NULL,
  expires_at_ms BIGINT,
  reference_notional_usd DOUBLE NOT NULL,
  quote_json VARCHAR NOT NULL,
  status VARCHAR NOT NULL,
  reason_code VARCHAR
);
CREATE TABLE IF NOT EXISTS sl_hedge_plan (
  plan_id VARCHAR PRIMARY KEY,
  symbol VARCHAR NOT NULL,
  canonical_id VARCHAR NOT NULL,
  mode VARCHAR NOT NULL,
  status VARCHAR NOT NULL,
  target_hedge_ratio DOUBLE NOT NULL,
  futures_notional_usd DOUBLE NOT NULL,
  futures_contract_qty DOUBLE NOT NULL,
  canonical_futures_qty DOUBLE NOT NULL,
  spot_venue VARCHAR NOT NULL,
  spot_symbol VARCHAR,
  spot_chain VARCHAR,
  spot_contract VARCHAR,
  target_spot_qty DOUBLE NOT NULL,
  target_spot_notional_usd DOUBLE,
  leverage DOUBLE,
  margin_mode VARCHAR,
  margin_usd DOUBLE,
  liquidation_price DOUBLE,
  liquidation_price_source VARCHAR,
  planned_hold_days INTEGER,
  fcs_snapshot_id VARCHAR,
  simulation_id VARCHAR NOT NULL,
  client_request_id VARCHAR NOT NULL UNIQUE,
  plan_safety_score DOUBLE,
  plan_config_json VARCHAR NOT NULL,
  created_at_ms BIGINT NOT NULL,
  activated_at_ms BIGINT,
  closed_at_ms BIGINT,
  updated_at_ms BIGINT NOT NULL,
  plan_version BIGINT NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS sl_hedge_leg (
  leg_id VARCHAR PRIMARY KEY,
  plan_id VARCHAR NOT NULL,
  leg_type VARCHAR NOT NULL,
  venue VARCHAR NOT NULL,
  side VARCHAR NOT NULL,
  qty DOUBLE NOT NULL,
  canonical_qty DOUBLE NOT NULL,
  avg_entry_price DOUBLE,
  actual_fee_usd DOUBLE,
  actual_gas_usd DOUBLE,
  opened_at_ms BIGINT,
  closed_qty DOUBLE NOT NULL DEFAULT 0,
  avg_exit_price DOUBLE,
  exit_fee_usd DOUBLE,
  exit_gas_usd DOUBLE,
  closed_at_ms BIGINT,
  state VARCHAR NOT NULL,
  updated_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS sl_hedge_monitor_snapshot (
  snapshot_id VARCHAR PRIMARY KEY,
  plan_id VARCHAR NOT NULL,
  as_of_ms BIGINT NOT NULL,
  actual_hedge_ratio DOUBLE,
  residual_short_notional_usd DOUBLE,
  mark_price DOUBLE,
  spot_price DOUBLE,
  current_basis_pct DOUBLE,
  basis_pnl_usd DOUBLE,
  estimated_settled_funding_usd DOUBLE,
  projected_next_funding_usd DOUBLE,
  spot_pnl_usd DOUBLE,
  futures_pnl_usd DOUBLE,
  known_cost_usd DOUBLE,
  estimated_exit_cost_usd DOUBLE,
  net_pnl_before_exit_usd DOUBLE,
  estimated_net_pnl_after_exit_usd DOUBLE,
  liquidation_distance DOUBLE,
  exit_liquidity_json VARCHAR,
  source_meta_json VARCHAR NOT NULL,
  quality_json VARCHAR NOT NULL,
  metrics_json VARCHAR NOT NULL,
  safety_score DOUBLE,
  status VARCHAR NOT NULL,
  created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS sl_hedge_alert (
  alert_id VARCHAR PRIMARY KEY,
  plan_id VARCHAR NOT NULL,
  code VARCHAR NOT NULL,
  severity VARCHAR NOT NULL,
  state VARCHAR NOT NULL,
  opened_at_ms BIGINT NOT NULL,
  last_seen_at_ms BIGINT NOT NULL,
  acknowledged_at_ms BIGINT,
  resolved_at_ms BIGINT,
  dedup_key VARCHAR NOT NULL,
  episode INTEGER NOT NULL,
  recommended_action VARCHAR NOT NULL,
  context_json VARCHAR NOT NULL,
  UNIQUE(plan_id, dedup_key, episode)
);
CREATE INDEX IF NOT EXISTS idx_sl_hedge_plan_status
ON sl_hedge_plan(status, updated_at_ms, plan_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sl_hedge_leg_plan_type
ON sl_hedge_leg(plan_id, leg_type);
CREATE INDEX IF NOT EXISTS idx_sl_spot_venue_lookup
ON sl_spot_venue_snapshot(canonical_id, venue, as_of_ms);
CREATE INDEX IF NOT EXISTS idx_sl_hedge_monitor_plan_time
ON sl_hedge_monitor_snapshot(plan_id, as_of_ms);
CREATE INDEX IF NOT EXISTS idx_sl_hedge_alert_plan_state
ON sl_hedge_alert(plan_id, state, last_seen_at_ms);
