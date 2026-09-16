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
from sqlglot.errors import SqlglotError

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


def recovery_signature(sql):
    """Result-shape grouping, not a proof of SQL/business equivalence.

    Resolve output columns through aliases/CTEs. Keep aggregates, DISTINCT and
    grouping dimensions; ignore literals, filters, ordering and LIMIT so routine
    optimization does not buy another retry budget. Pure calculations keep their
    own database-local group. The request's turn budget bounds shape changes.
    """
    from sqlglot import exp, parse_one
    from sqlglot.optimizer.scope import Scope, build_scope
    tables = source_tables(sql)
    if not tables:
        return {"tables": [], "outputs": ["calculation"]}
    root = build_scope(parse_one(sql, read="sqlite"))

    def ordered(values):
        unique = {json.dumps(v, sort_keys=True): v for v in values}
        return [unique[k] for k in sorted(unique)]

    def describe(node, scope, visiting=frozenset()):
        if isinstance(node, exp.Alias):
            return describe(node.this, scope, visiting)
        if isinstance(node, exp.Column):
            name = node.name.lower()
            sources = {k.lower(): v[1] for k, v in scope.selected_sources.items()}
            targets = ([sources[node.table.lower()]] if node.table.lower() in sources
                       else list(sources.values()))
            resolved = []
            for source in targets:
                if isinstance(source, Scope):
                    marker = (id(source), name)
                    if marker in visiting:
                        resolved.append(["recursive_column", name])
                        continue
                    for i, projection in enumerate(source.expression.selects):
                        alias = (source.outer_columns[i] if i < len(source.outer_columns)
                                 else projection.alias_or_name)
                        if alias.lower() == name:
                            resolved.append(describe(projection, source, visiting | {marker}))
                            break
                    else:
                        if any(p.is_star for p in source.expression.selects):
                            resolved.append(describe(exp.column(name), source, visiting | {marker}))
                        else:
                            resolved.append(["derived_column", name])
                elif isinstance(source, exp.Table):
                    resolved.append(["column", source.name.lower(), name])
            if len(resolved) == 1:
                return resolved[0]
            return ["references", ordered(resolved or [["column", name]])]
        if isinstance(node, exp.AggFunc):
            return ["aggregate", node.key, bool(node.find(exp.Distinct)),
                    ordered(describe(c, scope, visiting) for c in node.find_all(exp.Column))]
        if isinstance(node, exp.Star):
            return ["star"]
        if isinstance(node, exp.Query):
            child = next((s for s in scope.subquery_scopes if s.expression is node), None)
            if child:
                return shape(child)
            return ["subquery", canonical(node.sql(dialect="sqlite"))]
        # Stop at aggregates/columns rather than counting their children twice.
        parts = []
        def collect(expr):
            if isinstance(expr, (exp.AggFunc, exp.Column, exp.Star, exp.Query)):
                parts.append(describe(expr, scope, visiting))
            else:
                for child in expr.iter_expressions():
                    collect(child)
        collect(node)
        return ["expression", ordered(parts)]

    def shape(scope):
        if scope.union_scopes:
            return {"set_operation": scope.expression.key,
                    "branches": ordered(shape(s) for s in scope.union_scopes)}
        group = scope.expression.args.get("group")
        projections = scope.expression.selects
        def dimension(node):
            if isinstance(node, exp.Literal) and node.is_int and 0 < int(node.this) <= len(projections):
                node = projections[int(node.this)-1]
            elif isinstance(node, exp.Column) and not node.table:
                node = next((p for p in projections if p.alias.lower() == node.name.lower()), node)
            return describe(node, scope)
        return {"outputs": ordered(describe(p, scope) for p in scope.expression.selects),
                "group_by": ordered(dimension(p) for p in group.expressions) if group else [],
                "distinct": bool(scope.expression.args.get("distinct"))}

    return {"tables": tables, "result": shape(root) if root else canonical(sql)}


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

    Result shapes, not shared tables, determine recovery groups. Successful
    exploratory queries never clear another group's timeout history. This is a
    bounded heuristic, not proof that arbitrary rewrites preserve semantics.
    """
    def __init__(self, state, question, trace=None):
        self.state, self.question, self.trace = state, question, trace
        self.state.setdefault("groups", [])
        self.grant = None
        self.released_pending = None
        self._migrate_groups()

    def log(self, action, **data):
        if self.trace:
            self.trace.log(action, **data)

    def _group(self, sql, db_path):
        signature = recovery_signature(sql)
        db = str(Path(db_path).resolve())
        groups = self.state["groups"]
        for g in groups:
            if g["db"] == db and g.get("signature") == signature:
                return g
        g = {"id": secrets.token_hex(8), "db": db, "tables": signature["tables"],
             "signature": signature, "attempts": []}
        groups.append(g)
        return g

    def _migrate_groups(self):
        if self.state.get("grouping_version") == 2:
            return
        old_groups = self.state["groups"]
        self.state["groups"] = []
        for old in old_groups:
            for attempt in old["attempts"]:
                group = self._group(attempt["sql"], old["db"])
                group["attempts"].append(attempt)
                # Success fingerprints remain exact-SQL evidence, never grants.
                group["succeeded"] = sorted(set(group.get("succeeded", [])) |
                                            set(old.get("succeeded", [])))
        pending = self.state.get("pending")
        if pending:
            group = self._group(pending["sql"], pending["db_path"])
            pending.update(group_id=group["id"], tables=group["tables"],
                           attempts=list(group["attempts"]))
            # Only obsolete pre-execution blocks can resume without approval.
            # A query that actually timed out, or needs an extension, stays paused.
            attempted = any(a["fingerprint"] == pending["fingerprint"] for a in group["attempts"])
            if pending["kind"] == "approval" and not attempted and len(group["attempts"]) < MAX_ATTEMPTS:
                self.released_pending = self.state.pop("pending")
                self.log("sql_approval_superseded", approval_id=pending["id"],
                         reason="unexecuted query no longer shares unrelated timeout history")
        self.state["grouping_version"] = 2

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
        except (SqlglotError, ValueError, AttributeError, OSError) as exc:
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
