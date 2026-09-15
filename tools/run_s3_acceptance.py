"""S3 口径解释/治理闭环确定性验收(不调用 LLM)。"""

from copy import deepcopy
import hashlib
import json
import os
import shutil
import sys
import tempfile

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from semantic import registry  # noqa: E402
from semantic.metric_changes import (MetricChangeError, approve, list_pending,
                                     propose)  # noqa: E402

MDL = os.path.join(ROOT, "data", "ecommerce", "mdl.yaml")
DB = os.path.join(ROOT, "data", "ecommerce", "ecommerce.sqlite")


def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def main():
    reg = registry.load(MDL, db_path=DB)
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    description = registry.describe(reg)
    check("describe 含负责人/版本/生效时间",
          "负责人" in description and "版本" in description and "生效" in description,
          description)

    broken = deepcopy(reg.raw)
    broken["metrics"][0]["change_log"][-1]["version"] = 2
    try:
        registry.Registry(broken).validate()
        check("断裂 change_log 被拦截", False, "未抛出 RegistryError")
    except registry.RegistryError as exc:
        check("断裂 change_log 被拦截", True, str(exc))

    tmpdir = tempfile.mkdtemp(prefix="s3-acceptance-")
    try:
        tmp_mdl = os.path.join(tmpdir, "mdl.yaml")
        tmp_db = os.path.join(tmpdir, "ecommerce.sqlite")
        shutil.copy2(MDL, tmp_mdl)
        shutil.copy2(DB, tmp_db)
        before = digest(tmp_mdl)

        draft = propose(tmpdir, "GMV", "description", "商品支付金额（治理验收）",
                        "验收：退款字段已完成业务确认", "agent/s3-acceptance")
        after_propose = digest(tmp_mdl)
        pending = list_pending(tmpdir)
        check("提议生成 pending 且 mdl 未变",
              draft["status"] == "pending" and len(pending) == 1
              and before == after_propose and pending[0]["old"] == "商品支付金额",
              json.dumps(draft, ensure_ascii=False))

        approved = approve(tmpdir, draft["id"], approved_by="human/s3",
                           db_path=tmp_db)
        with open(tmp_mdl, encoding="utf-8") as fh:
            approved_data = yaml.safe_load(fh)
        gmv = next(m for m in approved_data["metrics"] if m["name"] == "GMV")
        with open(os.path.join(tmpdir, "metric_changes", draft["id"] + ".json"),
                  encoding="utf-8") as fh:
            approved_draft = json.load(fh)
        check("批准升版本并追加日志",
              approved["status"] == "approved" and gmv["version"] == 2
              and gmv["description"] == "商品支付金额（治理验收）"
              and len(gmv["change_log"]) == 2
              and gmv["change_log"][-1]["version"] == 2
              and approved_draft["status"] == "approved",
              json.dumps(approved, ensure_ascii=False))

        stale = propose(tmpdir, "GMV", "description", "第二次提议",
                        "验收过期保护", "agent/s3-acceptance")
        with open(tmp_mdl, encoding="utf-8") as fh:
            changed = yaml.safe_load(fh)
        changed["metrics"][0]["description"] = "外部人工已先改"
        with open(tmp_mdl, "w", encoding="utf-8") as fh:
            yaml.safe_dump(changed, fh, allow_unicode=True, sort_keys=False)
        try:
            approve(tmpdir, stale["id"], db_path=tmp_db)
            check("过期 old 被拒绝", False, "未抛出 MetricChangeError")
        except MetricChangeError as exc:
            check("过期 old 被拒绝", True, str(exc))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("=" * 60)
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  <- {detail}"))
    print("=" * 60)
    passed = sum(ok for _, ok, _ in checks)
    print(f"{passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
