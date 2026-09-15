"""电商店铺经营分析 - 合成数据 v2(对齐 PRD 第 10 章算例 + 带可发现的归因故事).

自有库 data/ecommerce/ecommerce.sqlite:
    orders/order_items/products (品类改家电/其他, 订单 10000→8000, 单均 100)
平台库 data/platform/platform_data.sqlite (mock 电商后台, 仅 MCP 可达):
    market_overview(行业大盘), shop_traffic(渠道流量), funnel(转化漏斗)
合成故事: 平台访客 +5%、我店总访客 +10%(自然+30%/付费-50%),
          我店转化率大跌(付费流量转化高却收缩) → GMV 1,000,000→800,000
"""
import csv
import json
import os
import random
import sqlite3
from datetime import datetime, timedelta

random.seed(11)
TODAY = datetime(2026, 9, 13)
PREV = [TODAY - timedelta(days=i) for i in range(14, 7, -1)]   # 08-30..09-05
CURR = [TODAY - timedelta(days=i) for i in range(7, 0, -1)]    # 09-06..09-12
PREV_STR = [d.strftime("%Y-%m-%d") for d in PREV]
CURR_STR = [d.strftime("%Y-%m-%d") for d in CURR]
OUT_E = "data/ecommerce"
OUT_P = "data/platform"

CATS = {"appliance": "家电", "other": "其他行业"}


def dstr(day):
    return day.strftime("%Y-%m-%d")


def build_shop():
    """自有店: GMV 1,000,000→800,000; 订单 10,000→8,000; 单均 100; 家电 60万→35万."""
    os.makedirs(OUT_E, exist_ok=True)
    products = []
    pid = 1
    pid_by = {}
    for cat, n in [("appliance", 12), ("other", 12)]:
        pid_by[cat] = []
        for i in range(1, n + 1):
            products.append({"product_id": f"P{pid:04d}", "product_name": f"{CATS[cat]}商品{i:02d}",
                             "category": CATS[cat]})
            pid_by[cat].append(f"P{pid:04d}")
            pid += 1

    targets = {"prev": {"orders": 10000, "total": 1_000_000, "appliance": 600_000},
               "curr": {"orders": 8000, "total": 800_000, "appliance": 350_000}}

    orders, items = [], []
    oid = 1
    iid = 1
    for pk, days in [("prev", PREV_STR), ("curr", CURR_STR)]:
        tgt = targets[pk]
        n_cross = round(tgt["orders"] * 0.03)
        n_app = round((tgt["orders"] - n_cross) * tgt["appliance"] / tgt["total"])
        n_oth = tgt["orders"] - n_cross - n_app
        parts = {}
        # 先定跨品类订单(其金额要从单品类配平目标中扣除)
        for _ in range(n_cross):
            oid_s = f"O{oid:07d}"
            oid += 1
            day = random.choice(days)
            orders.append({"order_id": oid_s, "shop_id": 1,
                           "pay_time": f"{day} {random.randint(8,23):02d}:{random.randint(0,59):02d}:00",
                           "status": "paid", "kind": "cross"})
            total = random.randint(70, 130)
            a = round(total * random.uniform(0.35, 0.65))
            parts[oid_s] = {"appliance": a, "other": total - a}
        cross_c = sum(p["appliance"] for p in parts.values())
        cross_o = sum(p["other"] for p in parts.values())
        for cat, target in [("appliance", tgt["appliance"] - cross_c),
                            ("other", tgt["total"] - tgt["appliance"] - cross_o)]:
            only_n = {"appliance": n_app, "other": n_oth}[cat]
            base = [random.randint(60, 140) for _ in range(only_n)]
            diff = target - sum(base)
            add = diff // len(base) if base else 0
            base = [b + add for b in base]
            base[0] += target - sum(base)
            assert all(b > 0 for b in base) and sum(base) == target
            for amt in base:
                oid_s = f"O{oid:07d}"
                oid += 1
                day = random.choice(days)
                orders.append({"order_id": oid_s, "shop_id": 1,
                               "pay_time": f"{day} {random.randint(8,23):02d}:{random.randint(0,59):02d}:00",
                               "status": "paid", "kind": cat})
                parts[oid_s] = {cat: amt}
        for _ in range(150):                                   # 未支付防呆
            oid_s = f"O{oid:07d}"
            oid += 1
            day = random.choice(days)
            orders.append({"order_id": oid_s, "shop_id": 1,
                           "pay_time": f"{day} {random.randint(8,23):02d}:{random.randint(0,59):02d}:00",
                           "status": "created", "kind": "appliance"})

        for o in orders[-len(parts):]:
            pass
        # 写明细(仅 paid)
        paid_ids = {o["order_id"] for o in orders if o["status"] == "paid"
                    and o["order_id"] in parts}
        for oid_s, cats in parts.items():
            for cat, amt in cats.items():
                if amt > 0:
                    items.append({"order_item_id": f"I{iid:08d}",
                                  "order_id": oid_s, "product_id": random.choice(pid_by[cat]),
                                  "category": CATS[cat], "amount": amt})
                    iid += 1
        # 核对
        paid = [o for o in orders if o["status"] == "paid" and o["order_id"] in parts]
        got = {c: sum(parts[o["order_id"]].get(c, 0) for o in paid) for c in ("appliance", "other")}
        assert sum(got.values()) == tgt["total"] and len(paid) == tgt["orders"], f"{pk} 配平失败"
        assert got["appliance"] == tgt["appliance"], f"{pk} 品类配平失败"

    for o in orders:
        o.pop("kind", None)
    orders.sort(key=lambda x: x["order_id"])
    with open(f"{OUT_E}/orders.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["order_id", "shop_id", "pay_time", "status"])
        w.writeheader(); w.writerows(orders)
    with open(f"{OUT_E}/order_items.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["order_item_id", "order_id", "product_id", "category", "amount"])
        w.writeheader(); w.writerows(items)
    with open(f"{OUT_E}/products.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["product_id", "product_name", "category"])
        w.writeheader(); w.writerows(products)

    conn = sqlite3.connect(f"{OUT_E}/ecommerce.sqlite")
    conn.execute("DROP TABLE IF EXISTS orders"); conn.execute("DROP TABLE IF EXISTS order_items")
    conn.execute("DROP TABLE IF EXISTS products")
    conn.execute("CREATE TABLE orders(order_id TEXT PRIMARY KEY, shop_id INTEGER, pay_time TEXT, status TEXT)")
    conn.execute("CREATE TABLE order_items(order_item_id TEXT PRIMARY KEY, order_id TEXT, product_id TEXT, category TEXT, amount INTEGER)")
    conn.execute("CREATE TABLE products(product_id TEXT PRIMARY KEY, product_name TEXT, category TEXT)")
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?)",
                     [(o["order_id"], o["shop_id"], o["pay_time"], o["status"]) for o in orders])
    conn.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)",
                     [(i["order_item_id"], i["order_id"], i["product_id"], i["category"], i["amount"]) for i in items])
    conn.executemany("INSERT INTO products VALUES (?,?,?)",
                     [(p["product_id"], p["product_name"], p["category"]) for p in products])
    conn.commit()
    # 核对
    sql = """SELECT SUM(CASE WHEN pay_time<'2026-09-06' THEN amount END),
                    SUM(CASE WHEN pay_time>='2026-09-06' THEN amount END)
             FROM order_items i JOIN orders o USING(order_id) WHERE o.status='paid'"""
    prev_t, curr_t = conn.execute(sql).fetchone()
    assert prev_t == 1_000_000 and curr_t == 800_000, f"{prev_t},{curr_t}"
    conn.close()
    print(f"[自有库] GMV {prev_t}→{curr_t} ✓ 订单 {targets['prev']['orders']}→{targets['curr']['orders']} ✓")


def build_platform():
    """平台库: 大盘访客 +5%; 自店渠道 自然+30%/付费-50%; 转化漏斗带故事."""
    os.makedirs(OUT_P, exist_ok=True)
    conn = sqlite3.connect(f"{OUT_P}/platform_data.sqlite")
    conn.execute("DROP TABLE IF EXISTS market_overview")
    conn.execute("DROP TABLE IF EXISTS shop_traffic")
    conn.execute("DROP TABLE IF EXISTS funnel")
    conn.execute("""CREATE TABLE market_overview(date TEXT, industry TEXT, platform_visitors INTEGER,
                     platform_gmv INTEGER, industry_conversion REAL, data_note TEXT)""")
    conn.execute("""CREATE TABLE shop_traffic(date TEXT, source TEXT, visitors INTEGER)""")
    conn.execute("""CREATE TABLE funnel(date TEXT, stage TEXT, count INTEGER)""")

    # 大盘(全行业=家电+其他): 访客 +5%/周, 转化率稳定 8%
    base_uv, base_gmv, base_cvr = 20_000_000, 5_000_000_000, 0.08
    for pk, days in [("prev", PREV_STR), ("curr", CURR_STR)]:
        for d in days:
            k = 1.0 + random.uniform(-0.02, 0.02)
            if pk == "curr":
                k *= 1.05                                   # 大盘 +5%
            v = int(base_uv / 7 * k)
            g = int(base_gmv / 7 * k)
            conn.execute("INSERT INTO market_overview VALUES (?,?,?,?,?,?)",
                         (d, "家电/其他行业合计", v, g, base_cvr * random.uniform(0.99, 1.01), "mock 大盘"))

    # 自店渠道流量(生意参谋口径): 总访客 = 自然流量 + 付费投放
    # 故事: 自然 +30%(搜索起量), 付费 -50%(投放预算收缩)
    prev_daily, curr_daily = 100_000 / 7, 110_000 / 7
    sources = {"手淘搜索": (30_000 / 7, 39_000 / 7),          # +30%
               "直通车(付费)": (55_000 / 7, 27_500 / 7),      # -50%  ← 故事主角
               "手淘推荐": (10_000 / 7, 12_000 / 7),
               "其他渠道": (5_000 / 7, 5_500 / 7)}
    # 100000/7=14286/天, 110000/7=15714/天 — 总量对齐, 结构变化
    drift = {"prev": {"手淘搜索": 30_000 / 7, "直通车(付费)": 55_000 / 7,
                      "手淘推荐": 10_000 / 7, "其他渠道": 5_000 / 7},
             "curr": {"手淘搜索": 55_000 / 7, "直通车(付费)": 27_500 / 7,
                      "手淘推荐": 20_000 / 7, "其他渠道": 7_500 / 7}}
    # 付费 -50%, 搜索 +83%, 推荐 +100%, 其他 +50% → 总量 100,000 → 110,000 (+10%, 对齐算例)
    # 故事: 付费投放收缩 → 高转化流量占比 55%→25% → 结构效应拉低整体转化率
    for pk, days in [("prev", PREV_STR), ("curr", CURR_STR)]:
        for d in days:
            for src, daily in drift[pk].items():
                v = int(daily * random.uniform(0.9, 1.1))
                conn.execute("INSERT INTO shop_traffic VALUES (?,?,?)", (d, src, max(v, 1)))
    # 漏斗(近7天汇总, 带故事: 访客多了但加购转化塌了)
    funnel_targets = {"prev": {"曝光": 400_000, "点击": 100_000, "详情页": 88_000,
                               "加购": 9_500, "支付": 10_000},
                      "curr": {"曝光": 430_000, "点击": 110_000, "详情页": 96_000,
                               "加购": 6_200, "支付": 8_000}}
    for pk, days in [("prev", PREV_STR), ("curr", CURR_STR)]:
        for stage, total in funnel_targets[pk].items():
            per = total / len(days)
            for d in days:
                conn.execute("INSERT INTO funnel VALUES (?,?,?)",
                             (d, stage, int(per * random.uniform(0.85, 1.15))))
    conn.commit()
    q = lambda s: conn.execute(s).fetchone()[0]
    pv = q("SELECT SUM(visitors) FROM shop_traffic WHERE date<'2026-09-06'")
    cv = q("SELECT SUM(visitors) FROM shop_traffic WHERE date>='2026-09-06'")
    mv_prev = q("SELECT SUM(platform_visitors) FROM market_overview WHERE date<'2026-09-06'")
    mv_curr = q("SELECT SUM(platform_visitors) FROM market_overview WHERE date>='2026-09-06'")
    print(f"[平台库] 自店访客 {pv}→{cv} ({(cv/pv-1)*100:+.1f}%)")
    print(f"[平台库] 大盘访客(7天) {mv_prev}→{mv_curr} ({(mv_curr/mv_prev-1)*100:+.1f}%)")
    conn.close()


if __name__ == "__main__":
    build_shop()
    build_platform()
    print("合成数据 v2 完成")
