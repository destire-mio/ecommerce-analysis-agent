"""Flask HTTP 适配层：把现有 agent_loop Runtime 暴露给本地 Vue 页面。

业务判断和取数仍由 ``agent_loop.run_agent`` 完成；本模块只负责请求校验、
响应整形和静态文件托管。
"""

import json
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_loop import run_agent  # noqa: E402


def _execution_sort_key(item):
    key = str(item[0])
    try:
        return (0, int(key.removeprefix("Q")))
    except ValueError:
        return (1, key)


def _evidence_and_charts(executions: dict):
    evidence = []
    charts = []
    for qn, entry in sorted((executions or {}).items(), key=_execution_sort_key):
        detail = entry.get("sql")
        if detail is None:
            detail = entry.get("args") or {}
        evidence.append({
            "qn": qn,
            "tool": entry.get("tool"),
            "detail": detail,
            "n_rows": entry.get("n_rows") if entry.get("n_rows") is not None else 0,
        })
        if entry.get("tool") != "chart":
            continue
        payload = entry.get("payload") or {}
        option = payload.get("option") or entry.get("option")
        if option is None:
            path = payload.get("path") or entry.get("path")
            if path:
                chart_path = Path(path)
                if not chart_path.is_absolute():
                    chart_path = ROOT / chart_path
                try:
                    with chart_path.open(encoding="utf-8") as fh:
                        option = json.load(fh)
                except (OSError, json.JSONDecodeError):
                    option = None
        if option is not None:
            charts.append(option)
    return evidence, charts


def create_app(runtime_runner=None):
    """创建 Flask app；``runtime_runner`` 便于冒烟/单元测试注入假的 Runtime。"""
    app = Flask(__name__)
    runner = runtime_runner or run_agent

    def runtime_response(result):
        evidence, charts = _evidence_and_charts(result.get("executions", {}))
        response = {"answer": result.get("answer", ""), "evidence": evidence,
                    "charts": charts, "session_id": result.get("session_id"),
                    "run_id": result.get("run_id"), "status": result.get("status", "error")}
        for field in ("reason", "approval"):
            if result.get(field):
                response[field] = result[field]
        return jsonify(response)

    @app.post("/query-approval")
    def query_approval():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("session_id"), str) or not payload["session_id"]:
            return jsonify(status="error", reason="审批需要 session_id"), 400
        if not isinstance(payload.get("id"), str) or payload.get("decision") not in ("approve", "deny"):
            return jsonify(status="error", reason="审批需要 id 和 approve/deny 决定"), 400
        try:
            result = runner("", bench="ecommerce", db_id="ecommerce", session_id=payload["session_id"],
                            approval={"id": payload["id"], "decision": payload["decision"], "seconds": payload.get("seconds", 60)})
        except Exception as exc:
            app.logger.exception("query approval failed")
            return jsonify(status="error", reason=str(exc)), 500
        return runtime_response(result)

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.get("/")
    def index():
        index_path = DIST / "index.html"
        if not index_path.exists():
            return ("前端尚未构建，请先在 frontend/ 执行 npm install && npm run build\n", 503,
                    {"Content-Type": "text/plain; charset=utf-8"})
        return send_from_directory(str(DIST), "index.html")

    @app.get("/assets/<path:filename>")
    def assets(filename):
        return send_from_directory(str(DIST / "assets"), filename)

    @app.post("/ask")
    def ask():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"status": "error", "reason": "请求体必须是 JSON 对象"}), 400
        question = payload.get("question")
        if not isinstance(question, str) or not question.strip():
            return jsonify({"status": "error", "reason": "question 不能为空"}), 400
        session_id = payload.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            return jsonify({"status": "error", "reason": "session_id 必须是字符串"}), 400
        try:
            result = runner(
                question.strip(), bench="ecommerce", db_id="ecommerce",
                session_id=session_id or None)
        except Exception as exc:  # Runtime 错误转成可观察的 HTTP 错误，不让进程退出
            app.logger.exception("agent runtime failed")
            return jsonify({"status": "error", "reason": str(exc)}), 500

        return runtime_response(result)

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8000")), threaded=True)
