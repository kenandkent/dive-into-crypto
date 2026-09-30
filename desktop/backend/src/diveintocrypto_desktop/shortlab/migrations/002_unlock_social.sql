-- Short-Lab Phase 5 schema (design section 19.2, migrations/002_unlock_social.sql).
--
-- Forward-only, idempotent: every statement uses IF NOT EXISTS and
-- `sl_schema_version` guarantees each migration file applies once.
-- `known_at_ms` is the first time the local process could know the event;
-- point-in-time replay must never use rows with `known_at_ms` later than
-- the score decision point.

CREATE TABLE IF NOT EXISTS sl_unlock_event (
  event_id VARCHAR NOT NULL,
  canonical_id VARCHAR NOT NULL,
  known_at_ms BIGINT NOT NULL,
  unlock_at_ms BIGINT NOT NULL,
  amount_tokens DOUBLE NOT NULL,
  allocation_type VARCHAR NOT NULL,
  source VARCHAR NOT NULL,
  fetched_at_ms BIGINT NOT NULL,
  PRIMARY KEY(event_id, known_at_ms)
);
CREATE TABLE IF NOT EXISTS sl_social_snapshot (
  snapshot_id VARCHAR PRIMARY KEY,
  canonical_id VARCHAR NOT NULL,
  as_of_ms BIGINT NOT NULL,
  fetched_at_ms BIGINT NOT NULL,
  source VARCHAR NOT NULL,
  metrics_json VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sl_unlock_asset_time ON sl_unlock_event(canonical_id, unlock_at_ms, known_at_ms);
CREATE INDEX IF NOT EXISTS idx_sl_social_asset_time ON sl_social_snapshot(canonical_id, as_of_ms);
