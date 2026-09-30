-- Short-Lab V1 schema (design section 19.2, migrations/001_init.sql).
--
-- Forward-only, idempotent: every statement uses IF NOT EXISTS and
-- `sl_schema_version` guarantees each migration file applies once.
-- V1 tables only; Phase 5/6 add 002_unlock_social.sql / 003_catalyst.sql.

CREATE TABLE IF NOT EXISTS sl_schema_version (
  version INTEGER PRIMARY KEY,
  applied_at_ms BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS sl_asset (
  canonical_id VARCHAR PRIMARY KEY,
  display_symbol VARCHAR NOT NULL,
  name VARCHAR,
  categories_json VARCHAR,
  created_at_ms BIGINT NOT NULL,
  updated_at_ms BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS sl_asset_mapping (
  futures_symbol VARCHAR PRIMARY KEY,
  canonical_id VARCHAR,
  spot_symbol VARCHAR,
  contract_multiplier DOUBLE,
  multiplier_source VARCHAR,
  coingecko_id VARCHAR,
  unlock_provider_id VARCHAR,
  social_provider_id VARCHAR,
  mapping_confidence VARCHAR NOT NULL,
  mapping_source VARCHAR NOT NULL,
  updated_at_ms BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS sl_contract_lifecycle (
  futures_symbol VARCHAR NOT NULL,
  observed_at_ms BIGINT NOT NULL,
  onboard_at_ms BIGINT,
  first_seen_ms BIGINT NOT NULL,
  delivery_at_ms BIGINT,
  contract_type VARCHAR,
  exchange_status VARCHAR,
  contract_multiplier DOUBLE,
  multiplier_source VARCHAR,
  PRIMARY KEY(futures_symbol, observed_at_ms)
);

CREATE TABLE IF NOT EXISTS sl_funding_event (
  symbol VARCHAR NOT NULL,
  funding_time_ms BIGINT NOT NULL,
  funding_rate DOUBLE NOT NULL,
  mark_price DOUBLE,
  PRIMARY KEY(symbol, funding_time_ms)
);

CREATE TABLE IF NOT EXISTS sl_fundamental_snapshot (
  snapshot_id VARCHAR PRIMARY KEY,
  canonical_id VARCHAR NOT NULL,
  as_of_ms BIGINT NOT NULL,
  fetched_at_ms BIGINT NOT NULL,
  market_cap_usd DOUBLE,
  fdv_usd DOUBLE,
  circulating_supply DOUBLE,
  total_supply DOUBLE,
  max_supply DOUBLE,
  ath_price DOUBLE,
  ath_date_ms BIGINT,
  source VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS sl_feature_snapshot (
  snapshot_id VARCHAR PRIMARY KEY,
  symbol VARCHAR NOT NULL,
  as_of_ms BIGINT NOT NULL,
  fundamental_snapshot_id VARCHAR,
  feature_version VARCHAR NOT NULL,
  features_json VARCHAR NOT NULL,
  source_meta_json VARCHAR NOT NULL,
  data_quality DOUBLE NOT NULL
);

CREATE TABLE IF NOT EXISTS sl_entry_snapshot (
  snapshot_id VARCHAR PRIMARY KEY,
  symbol VARCHAR NOT NULL,
  as_of_ms BIGINT NOT NULL,
  entry_version VARCHAR NOT NULL,
  dive_weights_hash VARCHAR NOT NULL,
  dive_engine_version VARCHAR NOT NULL,
  dive_config_hash VARCHAR NOT NULL,
  primary_tf VARCHAR NOT NULL,
  inputs_json VARCHAR NOT NULL,
  components_json VARCHAR NOT NULL,
  source_meta_json VARCHAR NOT NULL,
  entry_score DOUBLE,
  created_at_ms BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS sl_score_snapshot (
  snapshot_id VARCHAR PRIMARY KEY,
  generation_id VARCHAR NOT NULL,
  feature_snapshot_id VARCHAR NOT NULL,
  entry_snapshot_id VARCHAR,
  symbol VARCHAR NOT NULL,
  as_of_ms BIGINT NOT NULL,
  analysis_tier VARCHAR NOT NULL,
  profile VARCHAR NOT NULL,
  score_version VARCHAR NOT NULL,
  entry_version VARCHAR,
  feature_version VARCHAR NOT NULL,
  config_hash VARCHAR NOT NULL,
  ltss DOUBLE,
  entry_score DOUBLE,
  data_quality DOUBLE NOT NULL,
  candidate_status VARCHAR NOT NULL,
  execution_status VARCHAR NOT NULL,
  status VARCHAR NOT NULL,
  module_scores_json VARCHAR NOT NULL,
  vetoes_json VARCHAR NOT NULL,
  pauses_json VARCHAR NOT NULL,
  reasons_json VARCHAR NOT NULL,
  warnings_json VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS sl_forward_outcome (
  score_snapshot_id VARCHAR NOT NULL,
  horizon VARCHAR NOT NULL,
  outcome_status VARCHAR NOT NULL,
  reason_code VARCHAR,
  entry_ts_ms BIGINT,
  exit_ts_ms BIGINT,
  horizon_due_ms BIGINT NOT NULL,
  formula_version VARCHAR NOT NULL,
  cost_config_hash VARCHAR NOT NULL,
  funding_event_count INTEGER,
  funding_coverage DOUBLE,
  graded_at_ms BIGINT NOT NULL,
  entry_price DOUBLE,
  exit_price DOUBLE,
  price_short_return DOUBLE,
  funding_carry DOUBLE,
  fee_assumption DOUBLE,
  slippage_assumption DOUBLE,
  net_short_return DOUBLE,
  mae DOUBLE,
  mfe DOUBLE,
  PRIMARY KEY(score_snapshot_id, horizon, formula_version, cost_config_hash)
);

CREATE TABLE IF NOT EXISTS sl_job_run (
  job_id VARCHAR PRIMARY KEY,
  job_type VARCHAR NOT NULL,
  started_at_ms BIGINT NOT NULL,
  finished_at_ms BIGINT,
  status VARCHAR NOT NULL,
  stats_json VARCHAR,
  error_code VARCHAR
);

-- Query indexes (design section 19). `data_quality` stays a score column so
-- pagination/sorting never parses JSON.
CREATE INDEX IF NOT EXISTS idx_sl_score_generation ON sl_score_snapshot(generation_id, status, ltss, entry_score, data_quality, symbol, snapshot_id);
CREATE INDEX IF NOT EXISTS idx_sl_score_symbol_time ON sl_score_snapshot(symbol, as_of_ms);
CREATE INDEX IF NOT EXISTS idx_sl_feature_symbol_time ON sl_feature_snapshot(symbol, as_of_ms);
CREATE INDEX IF NOT EXISTS idx_sl_entry_symbol_time ON sl_entry_snapshot(symbol, as_of_ms);
CREATE INDEX IF NOT EXISTS idx_sl_funding_symbol_time ON sl_funding_event(symbol, funding_time_ms);
CREATE INDEX IF NOT EXISTS idx_sl_outcome_status_due ON sl_forward_outcome(outcome_status, horizon_due_ms);
