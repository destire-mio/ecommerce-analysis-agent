"""NL2SQL Agent Runtime - 第 2 轮：最小 Agent Runtime.

最小循环: prompt+schema(记轨迹) -> 生成(JSON三字段) -> 门禁 -> 执行(3s超时)
         -> 成功结束 / SQL错自修<=3次 / 门禁连败<=3次停 / 429·网络单独重试<=3次
轨迹:     runs/run-<时间戳>/trace.jsonl, 一行一事件
停止:     各重试上限 + 单查询3s + Ctrl-C 优雅停止
恢复:     幂等(确定性输入, 重跑结果一致)

用法:
    python3 runtime.py --question "How many singers do we have?" --db concert_singer
    python3 runtime.py --exam 0            # 用考卷第 0 题
"""

import json
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime

from openai import OpenAI

EXAM_PATH = "data/exam.json"
MODEL = "deepseek-flash"
SQL_TIMEOUT = 3          # 单次 SQL 执行上限(秒)
MAX_REPAIR = 3           # SQL 错误自修次数上限
MAX_GATE_FAIL = 3        # 门禁连败上限
MAX_NET_RETRY = 3        # 429/网络错误重试上限
WRITE_WORDS = {"INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
               "ATTACH", "DETACH", "REPLACE", "VACUUM", "PRAGMA", "REINDEX"}


class GateError(Exception):
    """门禁打回."""

    def __init__(self, reason: str):
        super().__init__(f"GateError: {reason}")
        self.reason = reason


class LLMError(Exception):
    """LLM 输出不可用(拒绝/坏 JSON/网络耗尽)."""


class Trace:
    """轨迹: 一行一事件, JSONL. Ctrl-C 时保证已写行落盘."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.fh = open(path, "a")
        signal.signal(signal.SIGINT, self._on_sigint)

    def _on_sigint(self, sig, frame):
        self.fh.flush()
        self.log("stop", result="sigint, exiting gracefully")
        self.fh.close()
        print(f"\n[stopped] trace 已完整落盘: {self.path}")
        sys.exit(130)

    def log(self, action: str, **kw) -> None:
        event = {"ts": datetime.now().isoformat(timespec="milliseconds"), "action": action, **kw}
        self.fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.fh.flush()


BENCH_DIRS = {
    "spider": os.path.join("data", "spider_data", "database"),
    "bird": os.path.join("data", "minidev", "MINIDEV", "dev_databases"),
}


def resolve_db_path(db_id: str, bench: str = None) -> str:
    """按 bench 定向找库文件; 未指定时按注册顺序找."""
    roots = [(bench, BENCH_DIRS[bench])] if bench else list(BENCH_DIRS.items())
    for _, root in roots:
        p = os.path.join(root, db_id, f"{db_id}.sqlite")
        if os.path.exists(p):
            return p
    raise FileNotFoundError(f"DBError: database not found: {db_id}")


def build_profile(conn, db_id: str) -> dict:
    """列值画像: 低基数列取值清单 + NULL 比例 + 中基数文本列样本. 每库一次性, 可缓存."""
    profile = {}
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for t in tables:
        cols = [(r[1], r[2]) for r in conn.execute(f'PRAGMA table_info("{t}")')]
        tprof = {}
        for cname, ctype in cols:
            try:
                row = conn.execute(
                    f'SELECT count(*), count("{cname}"), count(DISTINCT "{cname}") FROM "{t}"').fetchone()
                total, non_null, distinct = row
            except sqlite3.Error:
                continue
            if non_null == 0:
                tprof[cname] = "全空列(勿用于过滤)"
                continue
            null_pct = round((total - non_null) * 100 / total, 1) if total else 0
            if distinct <= 20:
                vals = [r[0] for r in conn.execute(
                    f'SELECT DISTINCT "{cname}" FROM "{t}" WHERE "{cname}" IS NOT NULL LIMIT 20')]
                tprof[cname] = f"取值({distinct}种, NULL {null_pct}%): {vals}"
            elif distinct <= 200 and any(k in (ctype or '').upper() for k in ('CHAR', 'TEXT', 'CLOB')):
                samples = [r[0] for r in conn.execute(
                    f'SELECT "{cname}" FROM "{t}" WHERE "{cname}" IS NOT NULL LIMIT 3')]
                samples = [str(s)[:40] for s in samples]
                tprof[cname] = f"样本(NULL {null_pct}%): {samples}"
        if tprof:
            profile[t] = tprof
    return profile


def get_profile(db_path: str, bench: str, db_id: str) -> str:
    """带缓存的画像: data/profiles/<bench>_<db>.json, 库文件改动才重建."""
    os.makedirs("data/profiles", exist_ok=True)
    cache = os.path.join("data", "profiles", f"{bench or 'default'}_{db_id}.json")
    mtime = os.path.getmtime(db_path)
    if os.path.exists(cache):
        saved = json.load(open(cache))
        if saved.get("mtime") == mtime:
            return saved["profile"]
    conn = sqlite3.connect(db_path)
    try:
        profile = build_profile(conn, db_id)
    finally:
        conn.close()
    json.dump({"mtime": mtime, "profile": profile}, open(cache, "w"), ensure_ascii=False)
    return profile


def load_db_schema(db_path: str, profile: dict) -> str:
    """直接从 SQLite 库文件读 schema (sqlite_master + PRAGMA), 附列值画像."""
    conn = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        lines = []
        fks = []
        for t in tables:
            cols = [(r[1], r[5]) for r in conn.execute(f'PRAGMA table_info("{t}")')]
            col_names = [c for c, _ in cols]
            pk = [c for c, flag in cols if flag]
            if pk:
                fks.append(f"{t}.主键 = {', '.join(pk)}")
            for r in conn.execute(f'PRAGMA foreign_key_list("{t}")'):
                fks.append(f'{t}.{r[3]} -> {r[2]}.{r[4]}')
            lines.append(f"{t}({', '.join(col_names)})")
        prof_lines = [f"{t}.{col}: {v}" for t, tp in profile.items() for col, v in tp.items()]
        return ("表:\n" + "\n".join(lines)
                + "\n键与外键:\n  " + "\n  ".join(fks)
                + "\n列值画像(真实取值/样本, 过滤条件务必照抄):\n  " + "\n  ".join(prof_lines))
    finally:
        conn.close()


MAX_INDEX_CARDINALITY = 50000
MAX_VALUE_LENGTH = 200
MIN_FUZZY_LEN = 4
MAX_EDIT_DISTANCE = 2


def value_db_path(bench: str, db_id: str) -> str:
    return os.path.join("data", "profiles", f"{bench or 'default'}_{db_id}_values.db")


def build_value_index(db_path: str, bench: str, db_id: str) -> str:
    """值索引: 文本列的全部不同值 -> 侧库(精确表 + FTS5 trigram). 每库一次性."""
    vpath = value_db_path(bench, db_id)
    meta_path = vpath + ".meta.json"
    mtime = os.path.getmtime(db_path)
    if os.path.exists(meta_path) and os.path.exists(vpath):
        saved = json.load(open(meta_path))
        if saved.get("mtime") == mtime:
            return vpath
    if os.path.exists(vpath):
        os.remove(vpath)
    src = sqlite3.connect(db_path)
    vdb = sqlite3.connect(vpath)
    try:
        vdb.execute("CREATE TABLE vi(value TEXT PRIMARY KEY, value_lower TEXT, cnt INTEGER)")
        vdb.execute("CREATE INDEX ix_lower ON vi(value_lower)")
        vdb.execute("CREATE VIRTUAL TABLE vfts USING fts5(value, tokenize='trigram')")
        tables = [r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            cols = [(r[1], (r[2] or '').upper()) for r in src.execute(f'PRAGMA table_info("{t}")')]
            for cname, ctype in cols:
                if not any(k in ctype for k in ('CHAR', 'TEXT', 'CLOB')):
                    continue
                distinct = src.execute(f'SELECT count(DISTINCT "{cname}") FROM "{t}"').fetchone()[0]
                if distinct == 0 or distinct > MAX_INDEX_CARDINALITY:
                    continue
                maxlen = src.execute(
                    f'SELECT max(length(CAST("{cname}" AS TEXT))) FROM "{t}"').fetchone()[0]
                if maxlen and maxlen > MAX_VALUE_LENGTH:  # 大段文本列(XML等)不进索引
                    continue
                for val, cnt in src.execute(
                        f'SELECT "{cname}", count(*) FROM "{t}" WHERE "{cname}" IS NOT NULL GROUP BY "{cname}"'):
                    vdb.execute(
                        "INSERT OR REPLACE INTO vi(value, value_lower, cnt) VALUES (?,?,?)",
                        (str(val), str(val).lower(), cnt))
                    vdb.execute("INSERT INTO vfts(value) VALUES (?)", (str(val),))
        vdb.commit()
    finally:
        vdb.close()
        src.close()
    json.dump({"mtime": mtime}, open(meta_path, "w"))
    return vpath


def _levenshtein(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > MAX_EDIT_DISTANCE:
        return MAX_EDIT_DISTANCE + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _fuzzy_candidates(conn, lit: str) -> list:
    """确定性召回: 删除/换位变体逐个点查索引 + trigram 并集兜底, 再按编辑距离排序."""
    low = lit.lower()
    cand = set()
    # 1) 删除一个字母的变体 ('Froest' -> 'Frost')
    for i in range(len(low)):
        row = conn.execute("SELECT value FROM vi WHERE value_lower = ? LIMIT 1",
                           (low[:i] + low[i+1:],)).fetchone()
        if row:
            cand.add(row[0])
    # 2) 相邻两字母换位的变体 ('Froest' -> 'Forest')
    for i in range(len(low) - 1):
        row = conn.execute("SELECT value FROM vi WHERE value_lower = ? LIMIT 1",
                           (low[:i] + low[i+1] + low[i] + low[i+2:],)).fetchone()
        if row:
            cand.add(row[0])
    # 3) trigram 逐个召回兜底 (治替换型错别字)
    trigrams = [f'"{lit[i:i+3]}"' for i in range(max(0, len(lit) - 2))]
    for t in trigrams:
        for r in conn.execute("SELECT value FROM vfts WHERE vfts MATCH ? LIMIT 200", (t,)):
            cand.add(r[0])
    scored = [(c, _levenshtein(low, c.lower())) for c in cand]
    scored = [c for c, d in scored if d <= MAX_EDIT_DISTANCE and len(c) >= MIN_FUZZY_LEN]
    scored.sort(key=lambda c: _levenshtein(low, c.lower()))
    return scored[:3]


def literal_check(sql: str, vdb_path: str, trace: Trace) -> str:
    """门禁第 5 道: 字面量 vs 值索引. 大小写自愈, 模糊打回带候选."""
    conn = sqlite3.connect(vdb_path)
    try:
        literals = [l for l in set(re.findall(r"'([^']*)'", sql)) if l and not l.isdigit()]
        fixes, suggests = {}, {}
        for lit in literals:
            rows = conn.execute(
                "SELECT value FROM vi WHERE value_lower = ? LIMIT 2", (lit.lower(),)).fetchall()
            if rows:
                exact = [r[0] for r in rows if r[0] != lit]
                if len(exact) == 1:
                    fixes[lit] = exact[0]  # 确定性自愈: 仅大小写差异
                continue
            suggests[lit] = _fuzzy_candidates(conn, lit)
        for lit, canon in fixes.items():
            trace.log("gate", result="literal_fix", original=lit, canonical=canon)
            sql = sql.replace(f"'{lit}'", f"'{canon}'")
        if any(v for v in suggests.values()):
            detail = "; ".join(f"'{k}' 不存在, 候选: {v or '无相近值'}" for k, v in suggests.items())
            trace.log("gate", result="literal_suggest", detail=detail)
            raise GateError(f"literal mismatch: {detail}")
        return sql
    finally:
        conn.close()


def gate_check(sql: str) -> str:
    """门禁: 返回清洗后的 SQL, 不合格抛 GateError."""
    body = sql.strip().removeprefix("```sql").removesuffix("```").strip().rstrip(";")
    head = body.split(None, 1)[0].upper() if body else ""
    if head != "SELECT":
        raise GateError(f"only SELECT allowed, got '{head or 'empty'}'")
    if ";" in body:
        raise GateError("multiple statements not allowed")
    for w in WRITE_WORDS:
        if w in body.upper().split() or f"{w} " in body.upper():
            raise GateError(f"write keyword forbidden: {w}")
    # 防失控: 无 LIMIT 的 SELECT 自动加保护上限
    if "LIMIT" not in body.upper():
        body = f"SELECT * FROM ({body}) LIMIT 100000"
    # EXPLAIN 预检: 语法错误提前打回
    return body


def explain_precheck(sql: str, db_path: str, trace: Trace) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("EXPLAIN " + sql)
    except sqlite3.Error as e:
        trace.log("gate", result="explain_fail", error=str(e))
        raise GateError(f"explain failed: {e}")
    finally:
        conn.close()


def run_sql(sql: str, db_path: str) -> list:
    """执行 SQL, 3s 超时保护."""
    conn = sqlite3.connect(db_path, timeout=SQL_TIMEOUT)
    conn.execute("PRAGMA query_only=ON")  # 数据库级只读兜底
    result = []
    timer = threading.Timer(SQL_TIMEOUT, lambda: result.append(("timeout", None)))
    timer.start()
    try:
        result.append(("ok", conn.execute(sql).fetchall()))
    except sqlite3.Error as e:
        result.append(("error", str(e)))
    finally:
        timer.cancel()
        conn.close()
    kind, payload = result[0]
    if kind == "timeout":
        raise TimeoutError(f"SQL exceeded {SQL_TIMEOUT}s")
    if kind == "error":
        raise RuntimeError(f"SQLError: {payload}")
    return payload


def call_llm(question: str, schema: str, error_hint: str, trace: Trace) -> dict:
    """调 DeepSeek, JSON 三字段. 429/网络单独重试<=MAX_NET_RETRY."""
    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
PROMPT = """# Role
You are a senior data analyst. Translate the business question into a single SQLite query.

# Task
Answer the question based on the DATABASE SCHEMA. If EXTRA KNOWLEDGE is provided,
it contains domain background (e.g. real business meaning of fields) and must be consulted first.

# Output format (strict)
Output ONLY a JSON object with exactly three fields:
{{"sql": "...", "used_tables": ["..."], "explanation": "..."}}
- sql: a single, executable, read-only SQLite SELECT statement
- used_tables: names of tables actually used
- explanation: one short sentence in Chinese explaining your interpretation choices

# Interpretation conventions (important)
1. Decide DISTINCT by what the question asks for:
   - Entity lists ("which names / what kinds / list the X") = set semantics
     -> use DISTINCT (duplicates are noise)
   - Fact/record lists ("each event / every record / per transaction")
     = bag semantics -> keep duplicates, no DISTINCT
2. "both ... and ...", "meet all conditions" = set INTERSECTION (use INTERSECT or
   double filtering). Never use IN for this — IN means OR.
3. Match GROUP BY granularity to the subject of the question: "for each player" groups
   by player, "for each name" groups by name.
4. Select output columns matching what the question asks for; when ambiguous, prefer
   the primary key / id column.
5. Compute numbers as-is; do not round unless explicitly asked.

# Constraints
- Read-only: any write operation is forbidden.
- Table and column names must come from the DATABASE SCHEMA. Never invent names.
- When matching string values with =, append COLLATE NOCASE
  (e.g. WHERE name = 'forest' COLLATE NOCASE). Case-sensitive matching is a
  common source of silent empty results.

[DATABASE SCHEMA]
{schema}

[EXTRA KNOWLEDGE]
{evidence}

[QUESTION]
{question}

{error_hint}"""


def call_llm(question: str, schema: str, error_hint: str, trace: Trace, evidence: str = "") -> dict:
    """调 DeepSeek, JSON 三字段. 429/网络单独重试<=MAX_NET_RETRY."""
    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
    prompt = PROMPT.format(schema=schema, question=question, error_hint=error_hint, evidence=evidence or "（无）")
    trace.log("prompt", input=question, error_hint=error_hint.strip(), prompt=prompt)
    for attempt in range(1, MAX_NET_RETRY + 1):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                response_format={"type": "json_object"},
            )
            usage = getattr(resp, "usage", None)
            trace.log("llm", result="ok",
                      usage={"prompt_tokens": usage.prompt_tokens,
                             "completion_tokens": usage.completion_tokens,
                             "total_tokens": usage.total_tokens} if usage else None)
            raw = resp.choices[0].message.content
            obj = json.loads(raw)
            if not obj.get("sql"):
                raise LLMError("json missing 'sql' field")
            obj.setdefault("used_tables", [])
            obj.setdefault("explanation", "")
            return obj
        except LLMError:
            raise
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            trace.log("llm", result="bad_output", attempt=attempt, error=str(e))
            raise LLMError(f"LLMBadOutputError: {e}") from e
        except Exception as e:  # 429/网络等, 单独重试
            trace.log("llm", result="transient_error", attempt=attempt, error=str(e))
            if attempt >= MAX_NET_RETRY:
                raise LLMError(f"LLMNetworkError after {MAX_NET_RETRY} retries: {e}") from e
            time.sleep(2 ** attempt)
    raise LLMError("unreachable")


def run_task(question: str, db_id: str, trace: Trace, evidence: str = "", bench: str = None) -> dict:
    """最小循环: 生成 -> 门禁 -> 执行 -> 失败自修."""
    db_path = resolve_db_path(db_id, bench)
    profile = get_profile(db_path, bench, db_id)
    schema = load_db_schema(db_path, profile)
    trace.log("schema", db_id=db_id, schema=schema)
    error_hint = ""
    repair = 0
    gate_fails = 0
    vdb_path = build_value_index(db_path, bench, db_id)
    while True:
        obj = call_llm(question, schema, error_hint, trace, evidence)
        trace.log("generate", output=obj)
        try:
            sql = gate_check(obj["sql"])
            sql = literal_check(sql, vdb_path, trace)
            explain_precheck(sql, db_path, trace)
            trace.log("gate", result="pass", sql=sql)
            gate_fails = 0
        except GateError as e:
            gate_fails += 1
            trace.log("gate", result="rejected", reason=e.reason, gate_fails=gate_fails)
            if gate_fails >= MAX_GATE_FAIL:
                return {"status": "failed", "reason": f"gate rejected {gate_fails} times", "last": e.reason}
            error_hint = f"上次输出被安全门禁拒绝: {e.reason}。请修正后重新输出。"
            continue
        try:
            rows = run_sql(sql, db_path)
            trace.log("execute", result="ok", n_rows=len(rows), sample=rows[:3])
            return {"status": "ok", "sql": sql, "n_rows": len(rows), "sample": rows[:3],
                    "explanation": obj["explanation"]}
        except (RuntimeError, TimeoutError) as e:
            repair += 1
            trace.log("execute", result="error", attempt=repair, error=str(e))
            if repair >= MAX_REPAIR:
                return {"status": "failed", "reason": f"repair limit {MAX_REPAIR} reached", "last": str(e)}
            error_hint = f"你上次的 SQL 执行报错: {e}。请修正后重新输出。"


def main() -> int:
    argv = sys.argv
    evidence = ""
    bench = argv[argv.index("--bench") + 1] if "--bench" in argv else None
    exam_path = argv[argv.index("--exam-file") + 1] if "--exam-file" in argv else EXAM_PATH
    if "--exam" in argv:
        q = json.load(open(exam_path))[int(argv[argv.index("--exam") + 1])]
        question, db_id = q["question"], q["db_id"]
        evidence = q.get("evidence", "")
        bench = q.get("bench", bench)
    else:
        question = argv[argv.index("--question") + 1]
        db_id = argv[argv.index("--db") + 1]
        evidence = argv[argv.index("--evidence") + 1] if "--evidence" in argv else ""
    run_id = datetime.now().strftime("run-%Y%m%d-%H%M%S")
    trace_path = os.path.join("runs", run_id, "trace.jsonl")
    trace = Trace(trace_path)
    trace.log("start", question=question, db_id=db_id)
    out = run_task(question, db_id, trace, evidence, bench)
    trace.log("end", result=out)
    trace.fh.flush()
    print(json.dumps({"run": run_id, **out}, ensure_ascii=False))
    return 0 if out["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
