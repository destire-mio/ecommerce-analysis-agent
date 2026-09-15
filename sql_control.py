"""Bounded read-only SQLite execution and request-owned slow-query recovery.

Every SQL runs in its own process. Killing and reaping that process precedes a
timeout observation, so retries cannot leave earlier database work running.
"""
import contextlib
import contextvars
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time
from sqlglot.errors import ParseError

SHORT_SECONDS = 5
LONG_SECONDS = 60
MAX_ATTEMPTS = 3
_CURRENT = contextvars.ContextVar("sql_control", default=None)


class QueryControlError(Exception):
    def __init__(self, payload):
        self.payload = payload
        super().__init__(payload.get("reason", payload.get("error", "query error")))


def canonical(sql):
    import sqlglot
    return sqlglot.parse_one(sql, read="sqlite").sql(dialect="sqlite", normalize=True, comments=False)


def identity(sql, db_path, params=()):
    path = Path(db_path).resolve()
    stat = path.stat()
    payload = [str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
               canonical(sql), params]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def source_tables(sql):
    from sqlglot import exp, parse_one
    tree = parse_one(sql, read="sqlite")
    ctes = {n.alias_or_name.lower() for n in tree.find_all(exp.CTE)}
    return sorted({t.name.lower() for t in tree.find_all(exp.Table) if t.name.lower() not in ctes})


def run_bounded(sql, db_path, params=(), seconds=SHORT_SECONDS, limit=None):
    """No model-controlled deadline. Includes worker startup, SQL and result transfer."""
    started = time.monotonic()
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    request = json.dumps({"sql": sql, "db": str(Path(db_path).resolve()),
                          "params": params, "seconds": seconds, "limit": limit})
    timed_out = False
    try:
        output, stderr = proc.communicate(request, timeout=seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        output, stderr = proc.communicate()  # reap before returning / launching retry
    messages = []
    for line in output.splitlines():
        try:
            messages.append(json.loads(line))
        except ValueError:
            pass  # an interrupted result write cannot become partial success
    plan = next((m["plan"] for m in messages if "plan" in m), [])
    result = next((m["result"] for m in reversed(messages) if "result" in m), None)
    elapsed = round(time.monotonic() - started, 3)
    if timed_out:
        return {"error": "query_timeout", "reason": f"查询未在 {seconds} 秒预算内完成，执行已取消",
                "cancelled": True, "elapsed_seconds": elapsed, "timeout_seconds": seconds,
                "query_plan": plan}
    if result is None:
        return {"error": "query_worker_failed", "reason": stderr[-1000:] or f"exit={proc.returncode}"}
    result.update(elapsed_seconds=elapsed, query_plan=plan)
    return result


def _worker():
    r = json.load(sys.stdin)
    conn = sqlite3.connect(Path(r["db"]).as_uri()+"?mode=ro", uri=True,
                           timeout=min(1.0, r["seconds"]))
    conn.execute("PRAGMA query_only=ON")
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
    try:
        plan = conn.execute("EXPLAIN QUERY PLAN " + r["sql"], r["params"]).fetchall()
        print(json.dumps({"plan": plan}), flush=True)
        cur = conn.execute(r["sql"], r["params"])
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(r["limit"]+1) if r["limit"] is not None else cur.fetchall()
        if r["limit"] is not None and len(rows) > r["limit"]:
            result = {"error": "result_too_large", "limit": r["limit"], "reason": "请聚合或按稳定键分页，截断结果不作为完整结果"}
        else:
            result = {"status": "ok" if rows else "empty", "columns": cols,
                      "n_rows": len(rows), "rows_all": rows,
                      "rows": rows if len(rows) <= 20 else rows[:5]}
            if len(rows) > 20:
                result["note"] = f"共 {len(rows)} 行，此为前 5 行"
        print(json.dumps({"result": result}, ensure_ascii=False), flush=True)
    except sqlite3.Error as exc:
        print(json.dumps({"result": {"error": "sql_error", "reason": str(exc)}}), flush=True)
    finally:
        conn.close()


@contextlib.contextmanager
def scope(controller):
    token = _CURRENT.set(controller)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def execute(sql, db_path, params=(), limit=None, runner=None):
    control = _CURRENT.get()
    if control:
        return control.execute(sql, db_path, params, limit, runner=runner)
    return runner(SHORT_SECONDS) if runner else run_bounded(sql, db_path, params, limit=limit)


class Controller:
    """Counters are held by the request, not by SQL text or LLM-provided IDs.

    Overlapping base tables conservatively share a recovery group. Statements
    without base tables share a separate calculation group per database. Successful
    exploratory queries never clear timeout history. This is a resource grouping,
    not a proof that arbitrary SQL rewrites preserve business semantics.
    """
    def __init__(self, state, question, trace=None):
        self.state, self.question, self.trace = state, question, trace
        self.state.setdefault("groups", [])
        self.grant = None

    def log(self, action, **data):
        if self.trace:
            self.trace.log(action, **data)

    def _group(self, sql, db_path):
        tables = set(source_tables(sql))
        db = str(Path(db_path).resolve())
        groups = self.state["groups"]
        matches = [g for g in groups if g["db"] == db and
                   (bool(tables & set(g["tables"])) if tables else not g["tables"])]
        if not matches:
            g = {"id": secrets.token_hex(8), "db": db, "tables": sorted(tables), "attempts": []}
            groups.append(g)
            return g
        g = matches[0]
        for other in matches[1:]:
            g["attempts"].extend(other["attempts"])
            g.setdefault("succeeded", []).extend(other.get("succeeded", []))
            tables.update(other["tables"])
            groups.remove(other)
        g["tables"] = sorted(set(g["tables"]) | tables)
        return g

    def _pause(self, g, sql, db_path, params, limit, result, kind):
        pending = {"id": secrets.token_hex(16), "kind": kind, "question": self.question,
                   "sql": sql, "db_path": str(Path(db_path).resolve()), "params": params,
                   "limit": limit, "fingerprint": identity(sql, db_path, params),
                   "group_id": g["id"], "tables": g["tables"],
                   "attempts": list(g["attempts"]), "query_plan": result.get("query_plan", []),
                   "proposed_seconds": LONG_SECONDS,
                   "last_seconds": result.get("timeout_seconds", SHORT_SECONDS),
                   "reason": "短查询预算耗尽，请审核指定 SQL" if kind == "approval" else "批准的执行时间内未完成，请决定提高时限或结束任务"}
        self.state["pending"] = pending
        self.log("sql_approval_required", approval=pending)
        return {"error": "sql_approval_required", "reason": pending["reason"], "approval": pending}

    def approve(self, request_id, seconds):
        p = self.state.get("pending")
        if not p or p["id"] != request_id:
            raise ValueError("审批请求不存在或已使用")
        if isinstance(seconds, bool) or not isinstance(seconds, (float, int)) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("执行时间必须为正数")
        if p["kind"] == "approval" and seconds != LONG_SECONDS:
            raise ValueError(f"首次长查询审批限 {LONG_SECONDS} 秒")
        if p["kind"] == "extension" and seconds <= p["last_seconds"]:
            raise ValueError("继续执行需明确提高时限，或结束任务")
        if identity(p["sql"], p["db_path"], p["params"]) != p["fingerprint"]:
            raise ValueError("数据源或 SQL 已变化，需要重新核对，原审批不可使用")
        self.grant = {"fingerprint": p["fingerprint"], "seconds": seconds}
        del self.state["pending"]
        self.log("sql_approved", approval_id=request_id, seconds=seconds, fingerprint=p["fingerprint"])
        return p

    def execute(self, sql, db_path, params=(), limit=None, runner=None):
        pending = self.state.get("pending")
        if pending:
            return {"error": "sql_approval_required", "reason": pending["reason"], "approval": pending}
        try:
            key = identity(sql, db_path, params)
            g = self._group(sql, db_path)
        except (ParseError, ValueError, AttributeError, OSError) as exc:
            return {"error": "sql_error", "reason": str(exc)}
        granted = self.grant and key == self.grant["fingerprint"]
        seconds = self.grant["seconds"] if granted else SHORT_SECONDS
        if granted:
            self.grant = None  # exact statement, once, before execution
        else:
            previous = next((a for a in g["attempts"] if a["fingerprint"] == key), None)
            if previous and key not in g.get("succeeded", []):
                return {"error": "repeat_timed_out_query", "reason": "相同查询已超时；请根据执行计划改写，不重复执行", "query_plan": previous["query_plan"], "attempts_used": len(g["attempts"])}
            if len(g["attempts"]) >= MAX_ATTEMPTS and key not in g.get("succeeded", []):
                return self._pause(g, sql, db_path, params, limit, g["attempts"][-1], "approval")
        result = runner(seconds) if runner else run_bounded(sql, db_path, params, seconds, limit)
        self.log("sql_execution", group_id=g["id"], sql=sql, params=params,
                 db_path=str(Path(db_path).resolve()), seconds=seconds,
                 status=result.get("status", result.get("error")), elapsed_seconds=result.get("elapsed_seconds"),
                 query_plan=result.get("query_plan", []))
        if result.get("error") == "query_timeout":
            g["attempts"].append({"sql": sql, "params": params, "fingerprint": key,
                                  "query_plan": result.get("query_plan", []), "timeout_seconds": seconds,
                                  "elapsed_seconds": result["elapsed_seconds"]})
            if granted or len(g["attempts"]) >= MAX_ATTEMPTS:
                return self._pause(g, sql, db_path, params, limit, result, "extension" if granted else "approval")
            result.update(attempts_used=len(g["attempts"]), attempts_max=MAX_ATTEMPTS,
                          recovery_group=g["id"], how="检查执行计划、索引和重复扫描；保持店铺、时间、版本与指标定义，不以漏算换速度")
        elif result.get("status") in ("ok", "empty"):
            if key not in g.setdefault("succeeded", []):
                g["succeeded"].append(key)
        return result


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    _worker()
