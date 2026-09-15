"""人工审批指标口径草稿。

用法:
  .venv/bin/python tools/approve_metric_change.py <id>
  .venv/bin/python tools/approve_metric_change.py <id> --reject
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from semantic.metric_changes import MetricChangeError, approve  # noqa: E402


DEFAULT_ROOT = os.path.join(ROOT, "data", "ecommerce")


def main(argv=None):
    parser = argparse.ArgumentParser(description="审批或驳回指标口径变更草稿")
    parser.add_argument("change_id", help="草稿 id，如 mc-20260914-a1b2")
    parser.add_argument("--reject", action="store_true", help="驳回，不修改 mdl.yaml")
    parser.add_argument("--root", default=DEFAULT_ROOT,
                        help="指标根目录(默认 data/ecommerce)")
    parser.add_argument("--by", dest="approved_by", default=None,
                        help="审批人；不填则沿用 proposed_by 或 human")
    args = parser.parse_args(argv)
    db_path = os.path.join(args.root, "ecommerce.sqlite")
    if not os.path.exists(db_path):
        db_path = None
    try:
        result = approve(args.root, args.change_id, reject=args.reject,
                          approved_by=args.approved_by, db_path=db_path)
    except MetricChangeError as exc:
        print(json.dumps({"status": "error", "error": str(exc)},
                         ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
