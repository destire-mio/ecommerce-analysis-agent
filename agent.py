"""NL2SQL agent - 第 1 轮：材料与能力入口.

链路: 问题 -> generate_sql (DeepSeek) -> execute_sql (SQLite) -> 结果.
用法:
    python3 agent.py --mode mock    # 固定 Mock, 不调 API
    python3 agent.py --mode real    # 真调 DeepSeek
    python3 agent.py --mode fail    # 明确失败 (坏 SQL)
"""

import json
import os
import sqlite3
import sys

from openai import OpenAI

DATA_DIR = "data/spider_data"
EXAM_PATH = "data/exam.json"
MODEL = "deepseek-chat"


def load_db_schema(db_id: str) -> str:
    """从 tables.json 取库的表结构文本."""
    tables = json.load(open(os.path.join(DATA_DIR, "tables.json")))
    db = next(t for t in tables if t["db_id"] == db_id)
    lines = []
    for t in db["table_names_original"]:
        cols = [c[1] for c in db["column_names_original"] if c[0] == db["table_names_original"].index(t)]
        lines.append(f"{t}({', '.join(cols)})")
    pk = [db["column_names_original"][i][1] for i in db["primary_keys"]]
    def fk_col(idx: int) -> str:
        tbl, col = db["column_names_original"][idx]
        return f"{db['table_names_original'][tbl]}.{col}"

    fks = [
        f"{fk_col(a)} -> {fk_col(b)}"
        for a, b in db["foreign_keys"]
    ]
    return "表:\n" + "\n".join(lines) + "\n主键: " + ", ".join(pk) + "\n外键: " + "; ".join(fks)


def generate_sql(question: str, db_id: str, mock: bool = False) -> str:
    """能力入口 1: 自然语言 -> SQL."""
    if mock:
        return "SELECT count(*) FROM singer"

    client = OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com",
    )
    schema = load_db_schema(db_id)
    prompt = f"""你是 SQL 专家。根据数据库结构和问题, 写一条 SQLite 查询。
只输出 SQL 本身, 不要解释, 不要 markdown 代码块。

数据库结构:
{schema}

问题: {question}"""
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    return resp.choices[0].message.content.strip().removeprefix("```sql").removesuffix("```").strip()


def execute_sql(sql: str, db_id: str) -> list:
    """能力入口 2: SQL -> 结果. 出错抛明确异常."""
    db_path = os.path.join(DATA_DIR, "database", db_id, f"{db_id}.sqlite")
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DBError: database not found: {db_id}")
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.Error as e:
        raise RuntimeError(f"SQLError: {e}") from e
    finally:
        conn.close()
    return rows


def main() -> int:
    mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "mock"
    exam = json.load(open(EXAM_PATH))
    q = exam[0]

    if mode == "fail":
        try:
            execute_sql("SELECT count(*) FROM no_such_table", q["db_id"])
        except RuntimeError as e:
            print(f"== FAIL ==\n{{\"status\": \"error\", \"error\": \"{e}\"}}")
            return 1
        return 0

    if mode == "mock":
        sql = q["gold_sql"]  # Mock: 写死考卷题的标准答案, 不调 API
    else:
        sql = generate_sql(q["question"], q["db_id"])
    rows = execute_sql(sql, q["db_id"])
    print(f"== {mode.upper()} ==")
    print(json.dumps({"status": "ok", "mock": mode == "mock", "question": q["question"],
                      "sql": sql, "result": rows}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
