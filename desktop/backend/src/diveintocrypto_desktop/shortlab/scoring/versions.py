"""Short-Lab scoring / feature version constants (Task 10).

Design section 7.1: changing bins, windows or thresholds must bump the
version instead of silently recomputing old evidence. ``FEATURE_VERSION``
covers the deterministic math in ``shortlab/features/*``;
``SCORE_VERSION_LITE`` / ``SCORE_VERSION_FULL`` cover the LTSS weighting in
``shortlab/scoring/ltss.py``; ``ENTRY_VERSION`` is owned by Task 12.
"""

from __future__ import annotations

FEATURE_VERSION = "features-v1"
SCORE_VERSION_LITE = "ltss-lite-v1"
SCORE_VERSION_FULL = "ltss-full-v1"
ENTRY_VERSION = "entry-v1"
