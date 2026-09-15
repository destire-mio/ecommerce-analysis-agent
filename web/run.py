"""启动本地 Flask 服务。

用法: .venv/bin/python web/run.py [--port 8000] [--no-browser]
"""

import argparse
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.server import DIST, app  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description="启动电商分析助手本地 Web 服务")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    if not (DIST / "index.html").exists():
        print("前端 dist/index.html 不存在，请先执行: cd frontend && npm install && npm run build",
              file=sys.stderr)
        return 1

    url = f"http://{args.host}:{args.port}/"
    print(f"服务启动: {url}", flush=True)
    if not args.no_browser:
        # 只延迟到 Flask 开始监听后再打开，避免浏览器首个请求撞在启动窗口。
        def open_browser():
            time.sleep(0.8)
            webbrowser.open(url)

        threading.Thread(target=open_browser, daemon=True).start()
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
