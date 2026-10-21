# -*- coding: utf-8 -*-
"""
refresh_data.py — 麦当劳 MCP 真实数据刷新器

这是本项目"真实使用麦当劳 MCP"的证据：它通过 MCP 协议真实调用 4 个官方工具，
把菜单名称、门店实时价格、官方营养数据拉下来，重新生成 index.html 内嵌的数据块。

调用链：
  tools/list            → 发现工具与参数契约
  query-meals           → 当前门店在售餐品（code + 分类 + tags）
  query-meal-detail     → 餐品名称（参数名是 code）
  calculate-price       → 门店实时价格（items[].productCode，单位：分）
  list-nutrition-foods  → 160 条官方营养数据（热量/蛋白/脂肪/碳水/钠/钙）

用法：
  export MCD_MCP_TOKEN=你的token
  python refresh_data.py                 # 默认门店
  python refresh_data.py --store 3450314 # 指定门店
  python refresh_data.py --write         # 同时回写 index.html 数据块

安全：只调用只读工具 + calculate-price（算价无副作用），绝不调用 create-order /
     cancel-order / mall-create-order / draw-lottery 等任何写操作。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ENDPOINT = os.environ.get("MCD_MCP_ENDPOINT", "https://mcp.mcd.cn").rstrip("/")
TOKEN = os.environ.get("MCD_MCP_TOKEN", "").strip()
DEFAULT_STORE = os.environ.get("MCD_STORE_CODE", "3450314")
SEQ = [0]
GAP = 0.12

# 菜单池定义：4 类各 5 项，共 5×5×4×4=400 组合。
# 这里只声明"要哪些餐品"，价格/热量/在售状态全部由 MCP 实时提供。
WANTED = {
    "burger": ["巨无霸", "麦辣鸡腿汉堡", "板烧鸡腿堡", "双层吉士汉堡", "麦香鱼"],
    "side":   ["中薯条", "大薯条", "小薯条", "麦乐鸡 5块", "麦辣鸡翅 2块"],
    "drink":  ["可口可乐 中杯", "雪碧 中杯", "无糖可乐 中杯", "鲜煮咖啡 中杯"],
    "dessert": ["奥利奥麦旋风", "香芋派", "菠萝派", "圆筒冰淇淋"],
}
# 名称归一化别名（MCP 里的叫法可能与本表不同）
ALIAS = {
    "双层吉士汉堡": ["双层吉士汉堡", "双层吉士堡"],
    "麦乐鸡 5块": ["麦乐鸡 5块", "麦乐鸡5块", "麦乐鸡（5块）", "麦乐鸡"],
    "麦辣鸡翅 2块": ["麦辣鸡翅 2块", "麦辣鸡翅2块", "麦辣鸡翅（2块）", "麦辣鸡翅"],
    "鲜煮咖啡 中杯": ["鲜煮咖啡 中杯", "鲜煮咖啡中杯", "麦咖啡 中杯", "鲜萃咖啡 中杯"],
    "圆筒冰淇淋": ["圆筒冰淇淋", "原味圆筒冰淇淋", "圆筒"],
}


def _post(payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", "replace")


def _unwrap(raw):
    if not raw or not raw.strip():
        return None
    m = re.search(r"data:\s*(\{.*\})\s*$", raw, re.S)
    try:
        return json.loads(m.group(1) if m else raw)
    except Exception:
        return None


def _result(env):
    res = (env or {}).get("result") or {}
    if not isinstance(res, dict):
        return None
    if res.get("structuredContent"):
        return res["structuredContent"]
    for c in res.get("content") or []:
        t = (c or {}).get("text") or ""
        mm = re.search(r"(\{.*\}|\[.*\])", t, re.S)
        if mm:
            try:
                return json.loads(mm.group(1))
            except Exception:
                pass
    return res


def rpc(method, params=None):
    SEQ[0] += 1
    env = _unwrap(_post({"jsonrpc": "2.0", "id": SEQ[0], "method": method,
                         "params": params or {}}))
    time.sleep(GAP)
    return _result(env)


def tool(name, args=None):
    SEQ[0] += 1
    env = _unwrap(_post({"jsonrpc": "2.0", "id": SEQ[0], "method": "tools/call",
                         "params": {"name": name, "arguments": args or {}}}))
    time.sleep(GAP)
    return _result(env)


def parse_nutrition(text):
    """官方返回 '[160]{col,col}:' + 行 的表格文本"""
    if isinstance(text, list):
        return text
    if not isinstance(text, str):
        return []
    m = re.search(r"\[(\d+)\]\{([^}]+)\}:\s*\n(.*)$", text, re.S)
    if not m:
        return []
    cols = [c.strip() for c in m.group(2).split(",")]
    out = []
    for line in m.group(3).split("\n"):
        vals = [v.strip() for v in line.strip().split(",")]
        if len(vals) < len(cols):
            continue
        row = {}
        for i, c in enumerate(cols):
            v = vals[i]
            if v in ("null", ""):
                row[c] = None
            else:
                try:
                    row[c] = float(v) if "." in v else int(v)
                except ValueError:
                    row[c] = v
        out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=DEFAULT_STORE)
    ap.add_argument("--write", action="store_true", help="回写 index.html 数据块")
    args = ap.parse_args()

    if not TOKEN:
        print("!! 未设置 MCD_MCP_TOKEN")
        return 2

    print("=" * 64)
    print("麦当劳 MCP 真实数据刷新  store=%s" % args.store)
    print("=" * 64)

    rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "mcd-name-meal", "version": "1.0"}})

    # 1) 工具发现（留作 MCP_INTEGRATION.md 的证据）
    tl = rpc("tools/list")
    tools = (tl or {}).get("tools") or []
    print("[1] tools/list            → %d 个工具" % len(tools))

    # 2) 门店在售餐品
    meals = tool("query-meals", {"storeCode": args.store, "beType": 1, "orderType": 1}) or {}
    cats = ((meals.get("data") or {}).get("categories") or [])
    codes = []
    for c in cats:
        for m0 in c.get("meals") or []:
            if m0.get("code"):
                codes.append({"code": m0["code"], "category": c.get("name"), "tags": m0.get("tags") or []})
    print("[2] query-meals           → %d 分类 / %d 个在售餐品" % (len(cats), len(codes)))

    # 3) 餐品名称
    name2code = {}
    for item in codes:
        d = tool("query-meal-detail", {"storeCode": args.store, "code": item["code"],
                                       "beType": 1, "orderType": 1}) or {}
        nm = (d.get("data") or {}).get("name")
        if nm:
            item["name"] = nm
            name2code[nm] = item["code"]
    print("[3] query-meal-detail     → 解析到 %d 个名称" % len(name2code))

    # 4) 官方营养表
    nut_raw = tool("list-nutrition-foods")
    nut = parse_nutrition((nut_raw or {}).get("data"))
    nut_map = {r.get("productName"): r for r in nut if r.get("productName")}
    print("[4] list-nutrition-foods  → %d 条营养记录" % len(nut))

    # 5) 匹配 + 实时算价
    used_codes = set()

    def find_code(want):
        for cand in [want] + ALIAS.get(want, []):
            if cand in name2code and name2code[cand] not in used_codes:
                return cand, name2code[cand]
        # 模糊匹配：候选名必须包含完整目标名（或反之），并按长度差排序取最接近的，
        # 避免「香芋派」被匹配到名字就叫「派」的通用商品
        key = want.replace(" ", "")
        cands = []
        for nm in name2code:
            plain = nm.replace(" ", "")
            if key in plain or (len(plain) >= 3 and plain in key):
                cands.append(nm)
        cands.sort(key=lambda x: abs(len(x) - len(want)))
        for nm in cands:
            if name2code[nm] not in used_codes:
                return nm, name2code[nm]
        return None, None

    result = {"storeCode": args.store, "source": "McDonald's China MCP live", "pools": {}}
    missing = []
    for pool, wants in WANTED.items():
        rows = []
        for w in wants:
            nm, cd = find_code(w)
            if not cd:
                missing.append((pool, w, "未在门店菜单找到"))
                continue
            used_codes.add(cd)
            pr = tool("calculate-price", {"storeCode": args.store, "beType": 1, "orderType": 1,
                                          "items": [{"productCode": cd, "quantity": 1}]}) or {}
            pdata = pr.get("data") or {}
            cents = pdata.get("price")
            price = round(cents / 100.0, 1) if isinstance(cents, (int, float)) else None
            nr = nut_map.get(nm) or nut_map.get(w) or {}
            rows.append({
                "code": cd,
                "name": nm,
                "price": price,
                "calories": nr.get("energyKcal"),
                "protein": nr.get("protein"),
                "fat": nr.get("fat"),
                "carbohydrate": nr.get("carbohydrate"),
                "sodium": nr.get("sodium"),
            })
            print("    %-14s ¥%-6s %-5s kcal" % (nm, price, nr.get("energyKcal")))
        result["pools"][pool] = rows

    print("\n[5] 各池规模：" + ", ".join("%s=%d" % (k, len(v)) for k, v in result["pools"].items()))
    if missing:
        print("    未匹配：")
        for p, w, why in missing:
            print("      %s / %s（%s）" % (p, w, why))

    os.makedirs("data", exist_ok=True)
    with open("data/menu.real.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print("\n已写入 data/menu.real.json")

    # 6) 生成可直接粘贴进 index.html 的 JS
    js = "var MENU = " + json.dumps(
        {k: [{"code": r["code"], "name": r["name"], "price": r["price"], "calories": r["calories"]}
             for r in v] for k, v in result["pools"].items()},
        ensure_ascii=False, indent=2) + ";"
    with open("data/menu.js", "w", encoding="utf-8") as f:
        f.write(js)
    print("已写入 data/menu.js（可直接替换 index.html 中的 MENU 常量）")

    if args.write:
        path = "index.html"
        if os.path.exists(path):
            html = open(path, encoding="utf-8").read()
            new_html, n = re.subn(r"var MENU = \{.*?\n\};", js, html, count=1, flags=re.S)
            if n:
                open(path, "w", encoding="utf-8").write(new_html)
                print("已回写 index.html 的 MENU 常量")
            else:
                print("!! 未在 index.html 中找到 'var MENU = {...};'，请手动替换")
    return 0


if __name__ == "__main__":
    sys.exit(main())
