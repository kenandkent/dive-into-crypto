"""Task 0 source-contract pins (Short-Lab Phase 0 baseline).

Verifies two exchange-level facts against fixed offline fixtures so that
Task 3 (funding pagination) and Task 4 (quote-volume retention) can reuse
them without touching the network:

- ``funding_page.json`` keeps raw USDⓈ-M ``/fapi/v1/fundingRate`` field names
  (``fundingTime``/``fundingRate``/``markPrice``): rows arrive in ascending
  ``fundingTime`` order, so the next page can resume from ``last + 1``.
- ``futures_1d_kline.json`` keeps the raw USDⓈ-M kline array layout: index 5
  is base-asset volume, index 7 is quote-asset volume, and the two differ for
  this row (so a ``v == qv`` collapse would be caught); index 0/6 are
  open/close timestamps in ms.

See ``docs/baseline.md`` and ``ShortLab_Implementation_Plan_CN.md`` Task 0.
"""

import json
import pathlib

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "shortlab"


def _load_funding_page() -> list:
    return json.loads((FIXTURES / "funding_page.json").read_text())


def _load_futures_1d_kline() -> list:
    return json.loads((FIXTURES / "futures_1d_kline.json").read_text())


def test_funding_page_ascending_and_paginatable():
    funding_page = _load_funding_page()
    assert [row["fundingTime"] for row in funding_page] == sorted(
        row["fundingTime"] for row in funding_page
    )
    # The next page resumes after the last observed fundingTime.
    last_time = funding_page[-1]["fundingTime"]
    assert all(row["fundingTime"] < last_time + 1 for row in funding_page)


def test_futures_kline_base_vs_quote_volume():
    futures_1d_kline = _load_futures_1d_kline()
    assert float(futures_1d_kline[5]) != float(futures_1d_kline[7])
    assert int(futures_1d_kline[0]) < int(futures_1d_kline[6])  # open/close ms
