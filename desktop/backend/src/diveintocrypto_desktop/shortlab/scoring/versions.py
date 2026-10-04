"""Short-Lab scoring / feature version constants (Task 10, F05).

Design section 7.1: changing bins, windows or thresholds must bump the
version instead of silently recomputing old evidence. ``FEATURE_VERSION``
covers the deterministic math in ``shortlab/features/*`` *plus* the F05
production input contract (canonical-USD prices, complete ascending 30D
rates, aligned OI/price 7D windows, real two-sided book states); the bin
math itself is unchanged, so ``SCORE_VERSION_LITE`` / ``SCORE_VERSION_FULL``
stay ``ltss-lite-v1`` / ``ltss-full-v1`` and old math goldens replay
bit-identically. ``SCORE_VERSION_LITE`` / ``SCORE_VERSION_FULL`` cover the
LTSS weighting in ``shortlab/scoring/ltss.py``; ``ENTRY_VERSION`` is owned
by Task 12 and now marks the F05 observed/budget contract (``entry-v2``).
"""

from __future__ import annotations

FEATURE_VERSION = "features-v2"
SCORE_VERSION_LITE = "ltss-lite-v1"
SCORE_VERSION_FULL = "ltss-full-v1"
ENTRY_VERSION = "entry-v2"
