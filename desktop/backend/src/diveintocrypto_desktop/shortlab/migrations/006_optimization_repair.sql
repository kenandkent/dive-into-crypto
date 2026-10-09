-- Short-Lab R01 schema (design D13, migrations/006_optimization_repair.sql).
--
-- Forward-only, idempotent: every statement uses IF NOT EXISTS and
-- `sl_schema_version` guarantees each migration file applies once.
-- 001-005 are frozen and must never be modified; R01 repair tables land
-- here (006); later structures use 007+.
-- One DDL transaction per file: all tables, indexes and the version row
-- commit together via repository._migrate_sync; any failure rolls back to
-- schema version 5 and 006 stays unavailable while 001-005 keep serving.
-- Application maintains `sl_hedge_snapshot_reference` integrity in the same
-- single-worker transaction (no DB-level FK); JSON columns hold canonical
-- repair-contract-v1 payloads with Decimal strings (D03/D18.2).

CREATE TABLE IF NOT EXISTS sl_market_observation (
  observation_id VARCHAR PRIMARY KEY, symbol VARCHAR NOT NULL,
  kind VARCHAR NOT NULL, source_as_of_ms BIGINT, known_at_ms BIGINT NOT NULL,
  value_json VARCHAR NOT NULL, meta_json VARCHAR NOT NULL, raw_sha256 VARCHAR NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sl_market_observation_cutoff
  ON sl_market_observation(symbol,kind,known_at_ms,source_as_of_ms);
CREATE TABLE IF NOT EXISTS sl_funding_schedule (
  schedule_id VARCHAR PRIMARY KEY, symbol VARCHAR NOT NULL,
  effective_from_ms BIGINT NOT NULL, effective_to_ms BIGINT,
  known_at_ms BIGINT NOT NULL, schedule_json VARCHAR NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sl_funding_schedule_cutoff
  ON sl_funding_schedule(symbol,known_at_ms,effective_from_ms);
CREATE TABLE IF NOT EXISTS sl_fx_observation (
  fx_id VARCHAR PRIMARY KEY, currency VARCHAR NOT NULL,
  source_as_of_ms BIGINT NOT NULL, known_at_ms BIGINT NOT NULL,
  rate_str VARCHAR NOT NULL, source_json VARCHAR NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sl_fx_observation_cutoff
  ON sl_fx_observation(currency,source_as_of_ms,known_at_ms);
CREATE TABLE IF NOT EXISTS sl_hedge_decision_snapshot (
  decision_id VARCHAR PRIMARY KEY, symbol VARCHAR NOT NULL,
  generated_at_ms BIGINT NOT NULL, expires_at_ms BIGINT NOT NULL,
  decision_policy_hash VARCHAR NOT NULL, decision_json VARCHAR NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sl_hedge_decision_symbol
  ON sl_hedge_decision_snapshot(symbol,generated_at_ms,decision_id);
CREATE TABLE IF NOT EXISTS sl_hedge_protection_confirmation (
  confirmation_id VARCHAR PRIMARY KEY, plan_id VARCHAR NOT NULL,
  plan_version BIGINT NOT NULL, client_request_id VARCHAR NOT NULL,
  confirmed_at_ms BIGINT NOT NULL, expires_at_ms BIGINT NOT NULL,
  confirmation_json VARCHAR NOT NULL, UNIQUE(plan_id,client_request_id));
CREATE INDEX IF NOT EXISTS idx_sl_protection_plan
  ON sl_hedge_protection_confirmation(plan_id,plan_version,confirmed_at_ms);
CREATE TABLE IF NOT EXISTS sl_strategy_entry_snapshot (
  entry_id VARCHAR PRIMARY KEY, cohort VARCHAR NOT NULL,
  symbol VARCHAR NOT NULL, source_snapshot_id VARCHAR NOT NULL,
  strategy VARCHAR NOT NULL, decision_as_of_ms BIGINT NOT NULL,
  executed_as_of_ms BIGINT, entry_json VARCHAR NOT NULL,
  UNIQUE(cohort,source_snapshot_id,strategy));
CREATE INDEX IF NOT EXISTS idx_sl_strategy_entry_time
  ON sl_strategy_entry_snapshot(cohort,symbol,decision_as_of_ms);
CREATE TABLE IF NOT EXISTS sl_strategy_quote_task (
  task_id VARCHAR PRIMARY KEY, entry_id VARCHAR NOT NULL,
  horizon_days INTEGER NOT NULL, purpose VARCHAR NOT NULL,
  due_ms BIGINT NOT NULL, status VARCHAR NOT NULL,
  task_json VARCHAR NOT NULL, updated_at_ms BIGINT NOT NULL,
  UNIQUE(entry_id,horizon_days,purpose));
CREATE INDEX IF NOT EXISTS idx_sl_quote_task_due
  ON sl_strategy_quote_task(status,due_ms,task_id);
CREATE INDEX IF NOT EXISTS idx_sl_fcs_current
  ON sl_funding_capture_snapshot(symbol,as_of_ms,created_at_ms,snapshot_id);
