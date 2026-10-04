-- Short-Lab F01 schema (design A6.2, migrations/004_core_completion.sql).
--
-- Forward-only, idempotent: every statement uses IF NOT EXISTS and
-- `sl_schema_version` guarantees each migration file applies once.
-- 001-003 are frozen and must never be modified; new tables/columns land
-- here (004) or in later sequential files (005, 006, ...).
-- Base target 4 creates the older 002/003 tables empty in order; that does
-- not mean FULL is enabled (schema and provider capability stay separate).
-- One DDL transaction per file: all tables, indexes and the version row
-- commit together; any failure rolls back to the previous schema version.

CREATE TABLE IF NOT EXISTS sl_config_snapshot (
  policy_hash VARCHAR PRIMARY KEY,
  config_hash VARCHAR NOT NULL,
  policy_version VARCHAR NOT NULL,
  canonical_json VARCHAR NOT NULL,
  created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS sl_identity_snapshot (
  identity_snapshot_id VARCHAR PRIMARY KEY,
  futures_symbol VARCHAR NOT NULL,
  canonical_id VARCHAR NOT NULL,
  mapping_version VARCHAR NOT NULL,
  observed_at_ms BIGINT NOT NULL,
  identity_json VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS sl_contract_rules_snapshot (
  snapshot_id VARCHAR PRIMARY KEY,
  symbol VARCHAR NOT NULL,
  source_as_of_ms BIGINT,
  known_at_ms BIGINT NOT NULL,
  rules_json VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS sl_funding_observation (
  observation_id VARCHAR PRIMARY KEY,
  symbol VARCHAR NOT NULL,
  funding_time_ms BIGINT NOT NULL,
  known_at_ms BIGINT NOT NULL,
  raw_json VARCHAR NOT NULL,
  interval_hours DOUBLE,
  interval_source VARCHAR,
  observation_status VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS sl_data_cursor (
  job_type VARCHAR NOT NULL,
  cursor_key VARCHAR NOT NULL,
  cursor_json VARCHAR NOT NULL,
  updated_at_ms BIGINT NOT NULL,
  PRIMARY KEY(job_type, cursor_key)
);
CREATE INDEX IF NOT EXISTS idx_sl_identity_time
ON sl_identity_snapshot(futures_symbol, observed_at_ms);
CREATE INDEX IF NOT EXISTS idx_sl_rules_time
ON sl_contract_rules_snapshot(symbol, known_at_ms);
CREATE INDEX IF NOT EXISTS idx_sl_funding_observation_time
ON sl_funding_observation(symbol, funding_time_ms, known_at_ms);
CREATE INDEX IF NOT EXISTS idx_sl_contract_seen
ON sl_contract_lifecycle(futures_symbol, observed_at_ms);
CREATE INDEX IF NOT EXISTS idx_sl_fundamental_asset_time
ON sl_fundamental_snapshot(canonical_id, as_of_ms);
CREATE INDEX IF NOT EXISTS idx_sl_evidence_score_time
ON sl_score_snapshot(as_of_ms, symbol);
