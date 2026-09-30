-- Short-Lab Phase 6 schema (design section 19.2, migrations/003_catalyst.sql).
--
-- Forward-only, idempotent: every statement uses IF NOT EXISTS and
-- `sl_schema_version` guarantees each migration file applies once.
-- `known_at_ms` is the first time the local process could know the event;
-- point-in-time replay must never use rows with `known_at_ms` later than
-- the score decision point.

CREATE TABLE IF NOT EXISTS sl_catalyst_event (
  event_id VARCHAR NOT NULL,
  canonical_id VARCHAR NOT NULL,
  known_at_ms BIGINT NOT NULL,
  announced_at_ms BIGINT NOT NULL,
  effective_at_ms BIGINT,
  event_type VARCHAR NOT NULL,
  severity VARCHAR NOT NULL,
  confidence DOUBLE NOT NULL,
  source_url VARCHAR,
  title VARCHAR NOT NULL,
  PRIMARY KEY(event_id, known_at_ms)
);
CREATE INDEX IF NOT EXISTS idx_sl_catalyst_asset_time ON sl_catalyst_event(canonical_id, effective_at_ms, known_at_ms);
