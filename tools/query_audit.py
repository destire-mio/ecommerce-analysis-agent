"""查询并过滤 runs/audit.jsonl 中的会话审计记录。

用法:
  .venv/bin/python tools/query_audit.py
  .venv/bin/python tools/query_audit.py --session s-xxx
  .venv/bin/python tools/query_audit.py --date 2026-09-15
"""

import argparse
import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(ROOT, "runs", "audit.jsonl")


def read_audit(path: str, session: str = None, day: str = None) -> list:
    records = []
    if not os.path.exists(path):
        return records
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print(f"跳过损坏审计行: {line_no}", file=sys.stderr)
                continue
            if session and record.get("session") != session:
                continue
            if day and not str(record.get("ts", "")).startswith(day):
                continue
            records.append(record)
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description="过滤 runs/audit.jsonl 审计记录")
    parser.add_argument("--session", help="按 session 精确过滤")
    parser.add_argument("--date", dest="day", help="按日期 YYYY-MM-DD 过滤")
    parser.add_argument("--file", default=DEFAULT_PATH, help="审计文件路径")
    args = parser.parse_args(argv)

    for record in read_audit(args.file, session=args.session, day=args.day):
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
