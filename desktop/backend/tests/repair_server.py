"""R15a isolated repair test server stub (independent process prototype).

Harness-only entry point: serves the R15a harness app on
``127.0.0.1:46409`` (``HARNESS_ORIGIN``). This is a ``Stub`` (Stb)
prototype; the full license/logic implementation lands in R15b. R15b
executes real acceptance against this server but never modifies these
harness files.

Isolation from the root ``tests/conftest.py`` global mocks (why this file
must run in a fresh interpreter):

- Root ``tests/conftest.py`` (project root) patches ``sys.modules`` for
  ``crypcodile`` / ``crypcodile.exchanges.binance.backfill`` and
  ``aiolimiter`` with ``MagicMock``, and monkey-patches ``httpx.AsyncClient``,
  ``httpx.get`` and ``websockets.connect`` to route through an in-memory
  ``TestClient``. Any test that imports that conftest inherits fake network.
- This server therefore runs in an **independent backend Python process**
  with ``cwd=desktop/backend`` and ``PYTHONPATH=desktop/backend/src``,
  without importing the root ``tests/conftest.py``. It uses real
  ``ShortLabRuntime`` / ``Service`` / ``Repo`` / ``scheduler`` (via
  ``repair_harness.create_repair_test_app``) and only the HTTP-provider layer
  returns R00 raw fixtures.
- Clients must use stdlib ``urllib`` (``urllib.request``) against
  ``http://127.0.0.1:46409``. ``httpx`` / ``websockets`` patched transports
  are forbidden for harness E2E because they would mask real socket
  isolation. This module itself never imports ``httpx``, ``websockets``,
  ``crypcodile`` or ``aiolimiter`` (static self-check asserts this).
- The server binds only loopback ``127.0.0.1`` (never ``0.0.0.0``) and never
  registers into any production entry point. All records live under the
  caller-provided test ``data_dir``. Harness control routes
  (``/test/harness/*``) exist only here, never in production.

Full license/logic implementation is deferred to R15b (``license``:
harness-conformance + browser + end-to-end PASS). This stub only proves the
isolated process boots, serves ``/api/health`` + ``/test/harness/state``,
and honors clock/scenario control without real network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

HARNESS_HOST = "127.0.0.1"
HARNESS_PORT = 46409
HARNESS_ORIGIN = f"http://{HARNESS_HOST}:{HARNESS_PORT}"

# Stub marker for grep/self-checks (Stb == Stub prototype, R15b owns logic).
HARNESS_SERVER_KIND = "Stb"
HARNESS_SERVER_LICENSE_NOTE = "license: full implementation pending R15b"


def _ensure_backend_src_on_path() -> Path:
    """Ensure ``diveintocrypto_desktop`` is importable from backend src."""
    here = Path(__file__).resolve()
    # File lives at desktop/backend/tests/repair_server.py; src is ../src.
    candidates = [
        here.parents[1] / "src",
        Path.cwd() / "src",
        Path.cwd() / "desktop" / "backend" / "src",
    ]
    for cand in candidates:
        init = cand / "diveintocrypto_desktop" / "__init__.py"
        if init.exists():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            return cand
    # Fallback: assume PYTHONPATH already provides the package.
    return Path(candidates[0])


def build_harness_app(scenario: str, data_dir: Path, clock: Any | None = None) -> tuple[Any, Any]:
    """Build the harness app for the isolated process (real Runtime)."""
    _ensure_backend_src_on_path()
    try:
        from tests.repair_harness import FakeClock, create_repair_test_app  # type: ignore[import-not-found]
    except Exception:
        from repair_harness import FakeClock, create_repair_test_app  # type: ignore[import-not-found]
    if clock is None:
        clock = FakeClock()
    app = create_repair_test_app(Path(data_dir), scenario, clock)
    return app, clock


def wait_for_ready(origin: str, timeout_s: float = 15.0) -> dict[str, Any]:
    """Poll ``/api/health`` via stdlib urllib until ready (no httpx)."""
    deadline = time.monotonic() + float(timeout_s)
    last_error: str | None = None
    url = origin.rstrip("/") + "/api/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 - loopback test harness
                body = resp.read().decode("utf-8", errors="replace")
                if resp.status in (200, 404):
                    try:
                        return {"status": resp.status, "body": json.loads(body) if body else {}}
                    except Exception:
                        return {"status": resp.status, "body": {"raw": body[:500]}}
        except Exception as exc:  # connection refused while booting
            last_error = f"{type(exc).__name__}: {exc}"[:200]
        time.sleep(0.2)
    raise TimeoutError(f"harness server not ready at {url}: {last_error}")


def fetch_harness_state(origin: str, timeout_s: float = 5.0) -> dict[str, Any]:
    """Fetch ``/test/harness/state`` via stdlib urllib (isolated-process proof)."""
    url = origin.rstrip("/") + "/test/harness/state"
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 - loopback test harness
        return json.loads(resp.read().decode("utf-8"))


def run_server(host: str = HARNESS_HOST, port: int = HARNESS_PORT, scenario: str = "normal",
               data_dir: Path | None = None, log_level: str = "warning") -> None:
    """Serve the harness app with uvicorn (loopback only, harness-only)."""
    _ensure_backend_src_on_path()
    import uvicorn

    if host != HARNESS_HOST:
        raise ValueError(f"harness server only binds {HARNESS_HOST} (got {host!r})")
    if int(port) != HARNESS_PORT:
        raise ValueError(f"harness server only serves {HARNESS_PORT} (got {port!r})")
    target = Path(data_dir) if data_dir is not None else Path(tempfile.mkdtemp(prefix="repair-harness-"))
    target.mkdir(parents=True, exist_ok=True)
    app, _clock = build_harness_app(scenario, target)
    config = uvicorn.Config(app, host=host, port=int(port), log_level=log_level)
    server = uvicorn.Server(config)
    server.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R15a isolated repair harness server (Stub; R15b owns logic).")
    parser.add_argument("--host", default=HARNESS_HOST, help="must stay 127.0.0.1")
    parser.add_argument("--port", type=int, default=HARNESS_PORT, help="must stay 46409")
    parser.add_argument("--scenario", default="normal",
                        choices=["normal", "negative-funding", "unknown-schedule", "slow-provider", "plan-switch", "plan-switchakit"])
    parser.add_argument("--data-dir", default=None, help="test data dir (records stay here)")
    parser.add_argument("--log-level", default="warning")
    args = parser.parse_args(argv)
    # Isolated conftest-mock note: this process must not import the root
    # tests/conftest.py mocks for crypcodile/aiolimiter/httpx; urllib only.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(var, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    run_server(host=args.host, port=args.port, scenario=args.scenario,
               data_dir=Path(args.data_dir) if args.data_dir else None,
               log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
