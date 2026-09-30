"""Short-Lab forward evidence package (Task 16, design section 21).

``grader.py`` prices 7D/30D/90D short outcomes from archived score snapshots;
``metrics.py`` aggregates them for the already-registered
``/api/short/evidence/summary`` route (Task 14 router, unchanged).
"""

from diveintocrypto_desktop.shortlab.evidence import grader, metrics  # noqa: F401
