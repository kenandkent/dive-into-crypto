"""Short-Lab: internal long/short research module (not a service).

Lives inside the existing Desktop FastAPI process; see
`docs/ShortLab_Detailed_Design_CN.md` section 2. Task 1 owns `models.py`,
`config.py` and `default.yaml`; Tasks 2/9/14 import the frozen DTOs from
`models.py` and the `load_shortlab_config` / `config_hash` helpers from
`config.py`.
"""

from diveintocrypto_desktop.shortlab import config, models  # noqa: F401
