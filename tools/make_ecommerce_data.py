"""电商店铺经营分析助手 - 阶段 1: 合成数据 + 可核对标准答案.

对齐《电商店铺经营分析助手开发方案》:
  - 一家店(shop_id=1), 单币种 CNY, 按支付时间+天统计
  - 近7天(2026-09-06~09-12): 商品支付金额 80万, 支付订单 4000, 单均 200
      服饰 35万(-25万), 家居 45万(+5万), 合计 -20万
  - 前7天(2026-08-30~09-05): 100万, 5000单, 单均 200; 服饰 60万, 家居 40万
  - 防呆埋点: 跨品类订单(金额分摊到行) / 未支付订单(不入口径) / 重复导入文件
  - 访问/退款/库存数据: 首期缺失(保留缺口, 用于"缺数如实报"验收)

产物: data/ecommerce/{products,orders,order_items,duplicate_orders}.csv
      data/ecommerce/指标说明.md, data/ecommerce/golden_cases.json
"""

import csv
import json
import os
import random
from datetime import datetime, timedelta

OUT = "data/ecommerce"
random.seed(7)

PERIOD_PREV = ("2026-08-30", "2026-09-05")   # 前7天
PERIOD_CURR = ("2026-09-06", "2026-09-12")   # 近7天

TARGET = {
    "prev": {"orders": 5000, "clothing": 600_000, "home": 400_000},
    "curr": {"orders": 4000, "clothing": 350_000, "home": 450_000},
}
N_PRODUCTS = {"clothing": 20, "home": 20}
CAT_NAME = {"clothing": "服饰", "home": "家居"}


def gen_orders(period_key: str, start: datetime, end: datetime, oid_start: int) -> tuple:
    """返回 (orders, cat_parts, oid_next): 金额精确配平到品类目标; 订单ID全局唯一."""
    tgt = TARGET[period_key]
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    orders, oid = [], oid_start

    def add(day, status, kind):
        nonlocal oid
        orders.append({"order_id": f"O{oid:06d}", "shop_id": 1,
                       "pay_time": (day + timedelta(hours=random.randint(8, 23),
                                                    minutes=random.randint(0, 59))).strftime("%Y-%m-%d %H:%M:%S"),
                       "status": status, "kind": kind})
        oid += 1

    n_total = tgt["orders"]
    n_cross = 80
    n_clothing = round((n_total - n_cross) * tgt["clothing"] / (tgt["clothing"] + tgt["home"]))
    n_home = n_total - n_cross - n_clothing

    for kind, count in [("cross", n_cross), ("clothing", n_clothing), ("home", n_home)]:
        for _ in range(count):
            add(random.choice(days), "paid", kind)
    for _ in range(120):                             # 未支付订单(不入口径, 防呆)
        add(random.choice(days), "created", random.choice(["clothing", "home", "cross"]))

    paid = [o for o in orders if o["status"] == "paid"]
    cross = [o for o in paid if o["kind"] == "cross"]

    # 跨品类订单: 总额 + clothing 部分(40%~60%)先定死, home = 总额 - clothing
    cat_parts = {o["order_id"]: {} for o in paid}
    for o in cross:
        total = random.randint(150, 250)
        cpart = round(total * random.uniform(0.4, 0.6))
        cat_parts[o["order_id"]] = {"clothing": cpart, "home": total - cpart}
    cross_c = sum(cat_parts[o["order_id"]]["clothing"] for o in cross)
    cross_h = sum(cat_parts[o["order_id"]]["home"] for o in cross)

    # 单品类订单: 均匀抽 + 差额均摊配平(基线 150~250, 每单摊 diff/n, 余数挂第一单)
    def alloc(only_orders: list, need: int, tag: str):
        n = len(only_orders)
        base = [random.randint(150, 250) for _ in range(n)]
        diff = need - sum(base)
        add = diff // n
        base = [b + add for b in base]
        rem = need - sum(base)
        base[0] += rem
        assert all(b > 0 for b in base), f"{tag} 配平后出现非正金额"
        assert sum(base) == need, f"{tag} 配平失败: {sum(base)} != {need}"
        return base

    for cat, target in [("clothing", tgt["clothing"]), ("home", tgt["home"])]:
        only = [o for o in paid if o["kind"] == cat]
        cross_part = sum(cat_parts[o["order_id"]][cat] for o in cross)
        base = alloc(only, target - cross_part, cat)
        for o, amt in zip(only, base):
            cat_parts[o["order_id"]] = {cat: amt}

    # 最终核对: 两个品类各自合计 = 目标
    for cat, target in [("clothing", tgt["clothing"]), ("home", tgt["home"])]:
        got = sum(cat_parts[o["order_id"]].get(cat, 0) for o in paid)
        assert got == target, f"{cat} 合计 {got} != {target}"
    n_paid_ids = len({o["order_id"] for o in paid})
    assert n_paid_ids == n_total, f"订单数 {n_paid_ids} != {n_total}"
    return orders, cat_parts, oid


def main():
    os.makedirs(OUT, exist_ok=True)
    products = []
    pid = 1
    pid_by_cat = {}
    for cat, n in N_PRODUCTS.items():
        pid_by_cat[cat] = []
        for i in range(1, n + 1):
            products.append({"product_id": f"P{pid:04d}",
                             "product_name": f"{CAT_NAME[cat]}商品{i:02d}",
                             "category": CAT_NAME[cat]})
            pid_by_cat[cat].append(f"P{pid:04d}")
            pid += 1

    orders, items = [], []
    oid_next = 1
    for period_key, (s, e) in [("prev", PERIOD_PREV), ("curr", PERIOD_CURR)]:
        start = datetime.strptime(s, "%Y-%m-%d")
        end = datetime.strptime(e, "%Y-%m-%d")
        o, cat_parts, oid_next = gen_orders(period_key, start, end, oid_next)
        orders += o
        for o in o:
            if o["status"] != "paid":
                continue                              # 未支付订单不入明细
            for cat, amt in cat_parts[o["order_id"]].items():
                if amt > 0:
                    items.append({"order_id": o["order_id"],
                                  "product_id": random.choice(pid_by_cat[cat]),
                                  "category": cat, "amount": amt})

    orders.sort(key=lambda x: x["order_id"])
    items.sort(key=lambda x: (x["order_id"], x["category"]))
    for o in orders:
        o.pop("kind", None)

    with open(f"{OUT}/products.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["product_id", "product_name", "category"])
        w.writeheader(); w.writerows(products)
    with open(f"{OUT}/orders.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["order_id", "shop_id", "pay_time", "status"])
        w.writeheader(); w.writerows(orders)
    with open(f"{OUT}/order_items.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["order_id", "product_id", "category", "amount"])
        w.writeheader(); w.writerows(items)
    with open(f"{OUT}/orders.csv") as f:                 # 重复导入防呆: 复制前 100 单
        dup = list(csv.DictReader(f))[:100]
    with open(f"{OUT}/duplicate_orders.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["order_id", "shop_id", "pay_time", "status"])
        w.writeheader(); w.writerows(dup)

    # ===== 核对(不调模型): 数字必须与方案示例一致 =====
    def q(period):
        s, e = {"prev": PERIOD_PREV, "curr": PERIOD_CURR}[period]
        paid = {o["order_id"] for o in orders
                if o["status"] == "paid" and s <= o["pay_time"][:10] <= e}
        amt = {"clothing": 0, "home": 0}
        for i in items:
            if i["order_id"] in paid:
                amt[i["category"]] += i["amount"]
        return amt["clothing"], amt["home"], len(paid)

    c1, h1, n1 = q("prev")
    c2, h2, n2 = q("curr")
    check = {"prev_total": c1 + h1, "prev_orders": n1, "prev_avg": round((c1 + h1) / n1, 2),
             "curr_total": c2 + h2, "curr_orders": n2, "curr_avg": round((c2 + h2) / n2, 2),
             "curr_clothing": c2, "curr_home": h2,
             "clothing_delta": c2 - c1, "home_delta": h2 - h1, "total_delta": (c2 + h2) - (c1 + h1)}
    ok = (check["prev_total"] == 1_000_000 and check["curr_total"] == 800_000
          and n1 == 5000 and n2 == 4000 and c2 == 350_000 and h2 == 450_000
          and check["curr_avg"] == 200.0 and check["clothing_delta"] == -250_000
          and check["home_delta"] == +50_000)
    print("核对:", json.dumps(check, ensure_ascii=False))
    print("与方案示例一致:", "✓" if ok else "✗ 不一致!")
    assert ok, "合成数据与方案标准答案不符, 禁止交付"

    # ===== 指标说明 =====
    with open(f"{OUT}/指标说明.md", "w") as f:
        f.write(f"""# 指标说明（演示版本, 正式使用需业务负责人确认）

- **商品支付金额**: 优惠后支付给商品的金额(order_items.amount 之和); 不含运费税费; 退款单列, 不从中扣减。
- **支付订单数**: status=paid 的订单按 order_id 去重计数(按支付时间落入统计区间)。
- **每单平均支付金额**: 商品支付金额 ÷ 支付订单数。
- **日期口径**: 订单支付时间(pay_time), 单一时区, 按天聚合。
- **数据覆盖**: 首期仅一家店(shop_id=1), 币种 CNY。
- **缺口**: 访问、退款、库存数据首期缺失——相关问题必须回答"数据缺失", 不得推断。

## 已知易错点
1. 跨品类订单(order_items 中同一 order_id 出现服饰+家居两行): 品类金额可相加,
   品类订单数**不可**相加代替店铺去重订单数。
2. 重复导入: duplicate_orders.csv 与 orders.csv 有 100 单重叠, 导入必须按 order_id 去重。
3. 未支付订单(status=created): 不计入任何支付口径。
""")

    # ===== Golden Cases(10 题) =====
    paid_curr = {o["order_id"] for o in orders
                 if o["status"] == "paid" and PERIOD_CURR[0] <= o["pay_time"][:10] <= PERIOD_CURR[1]}
    top = max((i["amount"], i["product_id"]) for i in items
              if i["category"] == "clothing" and i["order_id"] in paid_curr)
    golden = [
        {"id": 1, "q": "近7天商品支付金额是多少?", "expect": {"value": 800000, "unit": "元"}},
        {"id": 2, "q": "近7天比前7天变化多少?", "expect": {"delta": -200000, "pct": -20.0}},
        {"id": 3, "q": "哪个品类下降最多? 降了多少?", "expect": {"category": "服饰", "delta": -250000}},
        {"id": 4, "q": "近7天支付订单数是多少? 比前7天呢?", "expect": {"curr": 4000, "prev": 5000, "delta": -1000}},
        {"id": 5, "q": "近7天每单平均支付金额是多少? 变化呢?", "expect": {"curr": 200, "delta": 0}},
        {"id": 6, "q": "近7天服饰品类支付金额是多少?", "expect": {"value": 350000}},
        {"id": 7, "q": "近7天服饰里卖得最好的商品是哪个? 卖了多少?", "expect": {"product": top[1], "amount": top[0]}},
        {"id": 8, "q": "近7天访问量是多少? 转化率呢?", "expect": {"answer_type": "missing_data",
                 "must_say": "访问数据缺失, 无法给出转化率"}},
        {"id": 9, "q": "近7天品类订单数相加等于店铺订单数吗?", "expect": {"answer_type": "trap",
                 "must_say": "不等, 跨品类订单在两个品类各计一次, 店铺订单数需去重"}},
        {"id": 10, "q": "如果把 duplicate_orders.csv 也导入, 金额会变吗?", "expect": {"answer_type": "trap",
                 "must_say": "不会变, 导入必须按 order_id 去重"}},
    ]
    json.dump(golden, open(f"{OUT}/golden_cases.json", "w"), ensure_ascii=False, indent=1)
    print(f"Golden Cases 10 题已写入 {OUT}/golden_cases.json")
    print(f"服饰 Top1 商品: {top[1]} = {top[0]} 元")


if __name__ == "__main__":
    main()
