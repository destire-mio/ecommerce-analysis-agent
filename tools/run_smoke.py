"""M6 本地全栈轻冒烟：构建前端、启动 Flask、调用 /ask。"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(ROOT, "frontend")
DIST_INDEX = os.path.join(ROOT, "dist", "index.html")
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _ensure_frontend():
    node_modules = os.path.join(FRONTEND, "node_modules")
    if not os.path.isdir(node_modules):
        subprocess.run(["npm", "install", "--no-audit", "--no-fund"],
                       cwd=FRONTEND, check=True)
    if not os.path.exists(DIST_INDEX):
        subprocess.run(["npm", "run", "build"], cwd=FRONTEND, check=True)


def _wait_ready(url, process, timeout=30):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        if process.poll() is not None:
            break
        try:
            with LOCAL_OPENER.open(url + "/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.25)
    # 不能对仍在运行的子进程 stdout 调用 read()：它会等 EOF，掩盖真正的
    # readiness 错误。finally 中会终止服务；这里保留最后一次连接错误即可。
    raise RuntimeError(f"Flask 服务未就绪: {last_error}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="M6 Flask + Vue 轻冒烟")
    parser.add_argument("--ask-timeout", type=float, default=600,
                        help="/ask 最大等待秒数；默认 600，长请求不设短超时")
    args = parser.parse_args(argv)

    _ensure_frontend()
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["PORT"] = str(port)
    process = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "web", "run.py"),
         "--port", str(port), "--no-browser"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_ready(url, process)
        print("[INFO] Flask 已就绪，开始调用 /ask（可能需要几分钟）", flush=True)
        body = json.dumps({"question": "近7天商品支付金额是多少？"}).encode()
        request = urllib.request.Request(
            url + "/ask", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with LOCAL_OPENER.open(request, timeout=args.ask_timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"/ask HTTP {exc.code}: {detail}") from exc

        answer = str(payload.get("answer", ""))
        normalized = answer.replace(",", "").replace("，", "")
        if payload.get("status") != "ok":
            raise RuntimeError(f"/ask status 非 ok: {payload}")
        if "800000" not in normalized:
            raise RuntimeError(f"答案未包含 800000: {payload}")
        if not isinstance(payload.get("evidence"), list):
            raise RuntimeError(f"evidence 不是结构化列表: {payload}")
        print(f"[PASS] /ask status=ok, answer 含 800000, evidence={len(payload['evidence'])} 条")
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        raise SystemExit(1)
