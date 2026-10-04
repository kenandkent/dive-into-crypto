"""Entry point: start the local service, serve the UI, open the browser.

    uv run short-lab                 # serve + open the UI in your browser
    uv run short-lab --no-open       # serve only (no browser)
    uv run short-lab --port 46408
    uv run dive-desktop               # legacy compat alias, same entry
"""

from __future__ import annotations

import argparse
import threading
import time
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser(prog="short-lab", description="short-lab — Desktop Edition")
    parser.add_argument("--host", default="127.0.0.1")
    # 项目约定：所有需端口的服务一律使用 40000~60000 区间内的端口。
    parser.add_argument("--port", type=int, default=46408)
    parser.add_argument("--no-open", action="store_true", help="do not open a browser window")
    args = parser.parse_args()

    import uvicorn

    from diveintocrypto_desktop.api.app import app

    url = f"http://{args.host}:{args.port}/"
    if not args.no_open:
        def _open() -> None:
            time.sleep(1.2)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    print(f"short-lab  →  {url}  (Ctrl+C to stop)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
