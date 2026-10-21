# -*- coding: utf-8 -*-
"""
export_orders.py — 把「我自己的麦当劳历史订单」导成网页可读的 JSON

为什么需要这个脚本：
  麦当劳 MCP 服务端禁止浏览器跨域调用（对 https://mcp.mcd.cn 的 OPTIONS 预检
  返回 403，且响应里没有 Access-Control-Allow-Origin），所以网页永远不可能直连
  MCP 读订单。于是改成：本地脚本读真实订单 → 导出 JSON → 网页用 FileReader 导入
  → 纯前端计算。订单数据不出你的电脑，比把 token 交给网页更安全。

用法：
  export MCD_MCP_TOKEN=你的token && python export_orders.py
  export MCD_MCP_TOKEN=你的token && python export_orders.py --out my-orders.json
  # Windows PowerShell:  $env:MCD_MCP_TOKEN="你的token"; python export_orders.py

可选参数：
  --out PATH        输出文件路径，默认 my-orders.json
  --page-size N     每页条数，默认 50
  --max-pages N     最多翻多少页，默认 20（防止接口异常时死循环）

安全约定：
  1. token 只从环境变量 MCD_MCP_TOKEN 读取，绝不硬编码、绝不写入任何输出文件；
  2. 只调用只读工具 order-list，不调用任何下单 / 取消 / 支付类工具；
  3. 导出时做脱敏：不导出会员卡号、手机号、收货地址、beCode 等字段，
     只保留 orderId（无隐私）、下单时间、门店名、金额、餐品名，足以支撑换算与洞察。

协议细节与 refresh_data.py 完全一致（那个文件是实测能跑通的）：
  POST JSON-RPC 到 https://mcp.mcd.cn
  头：Authorization: Bearer <token> / Content-Type: application/json
      / Accept: application/json, text/event-stream
  响应可能是 SSE（带 "data: " 前缀）、纯 JSON、或 initialize 的 202 空响应，三种都兼容。
"""
import argparse
import datetime
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
SEQ = [0]
GAP = 0.12

# 被认为「不算消费」的订单状态（取消 / 关闭 / 未支付），导出时跳过，
# 否则会把没花的钱也算进总消费里。
SKIP_STATUS = ("取消", "已关闭", "未支付", "待支付", "已退款")


# ---------------------------------------------------------------------------
# 1. MCP 通信层
# ---------------------------------------------------------------------------
def _post(payload):
    """发一个 JSON-RPC 请求，返回原始响应文本（HTTP 错误也尽量把 body 取回来）。"""
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
    except urllib.error.URLError as e:
        raise RuntimeError("连不上 %s：%s" % (ENDPOINT, e.reason))


def _unwrap(raw):
    """把响应文本解成 JSON-RPC 信封（兼容 SSE / 纯 JSON / 空响应）。"""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    # a) 纯 JSON
    try:
        return json.loads(text)
    except Exception:
        pass
    # b) SSE：单条 data: {...}
    m = re.search(r"data:\s*(\{.*\})\s*$", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # c) SSE 多事件：逐行找最后一个可解析的 data:
    found = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            found = json.loads(payload)
        except Exception:
            continue
    return found


def _result(env):
    """取出业务数据：优先 result.structuredContent，否则从 content[0].text 里抠 JSON。"""
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


def _error_text(env):
    """从信封里提取错误描述，没有错误返回空串。"""
    err = (env or {}).get("error")
    if not err:
        return ""
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or err)
    return str(err)


def rpc(method, params=None):
    """调用一个 MCP 方法（initialize / tools/list ...）。"""
    SEQ[0] += 1
    env = _unwrap(_post({"jsonrpc": "2.0", "id": SEQ[0], "method": method,
                         "params": params or {}}))
    time.sleep(GAP)
    return env


def tool_raw(name, args=None):
    """调用一个 MCP 工具，返回原始 JSON-RPC 信封（需要检查 error 时用这个）。"""
    SEQ[0] += 1
    env = _unwrap(_post({"jsonrpc": "2.0", "id": SEQ[0], "method": "tools/call",
                         "params": {"name": name, "arguments": args or {}}}))
    time.sleep(GAP)
    return env


def tool(name, args=None):
    """调用一个 MCP 工具，直接返回已解包的业务数据。"""
    return _result(tool_raw(name, args))


# ---------------------------------------------------------------------------
# 2. 数据规整
# ---------------------------------------------------------------------------
def _to_amount(value):
    """把 MCP 返回的金额（字符串，单位元）转成保留两位小数的 float。"""
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value, default=1):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _clean_items(products):
    """把 orderProductList 规整成 [{name, quantity, combo[]}]。"""
    items = []
    for p in products or []:
        if not isinstance(p, dict):
            continue
        name = (p.get("productName") or p.get("name") or "").strip()
        if not name:
            continue
        combo = []
        for sub in p.get("comboItemList") or []:
            if not isinstance(sub, dict):
                continue
            sub_name = (sub.get("name") or sub.get("productName") or "").strip()
            if sub_name:
                combo.append(sub_name)
        items.append({
            "name": name,
            "quantity": _to_int(p.get("quantity"), 1),
            "combo": combo,
        })
    return items


def normalize_order(raw):
    """把一单 MCP 原始订单脱敏 + 规整成导出结构。

    只保留：orderId（无隐私）、createTime、storeName、amount、items。
    明确丢弃：beCode / storeCode 之外的会员信息 / 手机号 / 地址 / 支付流水等。
    """
    if not isinstance(raw, dict):
        return None
    status = str(raw.get("orderStatus") or "")
    if any(k in status for k in SKIP_STATUS):
        return None
    return {
        "orderId": str(raw.get("orderId") or ""),
        "createTime": str(raw.get("createTime") or ""),
        "storeName": str(raw.get("storeName") or "").strip(),
        "amount": _to_amount(raw.get("realTotalAmount")),
        "items": _clean_items(raw.get("orderProductList")),
    }


def _month_index(text):
    """'2025-11-10 17:16:24' → 绝对月序号，用于算跨度。"""
    m = re.match(r"^(\d{4})-(\d{2})", str(text or ""))
    if not m:
        return None
    return int(m.group(1)) * 12 + int(m.group(2))


def count_items(orders):
    """统计餐品出现次数。

    口径：把每单的 items[].name 与 items[].combo[] 名称摊平，
    同一单内同名只算一次（"N 次" = 出现在 N 单里），更符合直觉。
    """
    counts = {}
    for o in orders:
        seen = set()
        for it in o.get("items") or []:
            names = [it.get("name") or ""] + list(it.get("combo") or [])
            for nm in names:
                nm = (nm or "").strip()
                if not nm or nm in seen:
                    continue
                seen.add(nm)
                counts[nm] = counts.get(nm, 0) + 1
    return counts


def build_summary(orders):
    """计算摘要块（与网页 summary 字段一一对应）。"""
    times = sorted([o["createTime"] for o in orders if o["createTime"]])
    stores = set()
    for o in orders:
        if o["storeName"]:
            stores.add(o["storeName"])
    first_order = times[0] if times else ""
    last_order = times[-1] if times else ""
    a, b = _month_index(first_order), _month_index(last_order)
    month_span = (b - a + 1) if (a is not None and b is not None) else 0
    return {
        "orderCount": len(orders),
        "totalAmount": round(sum(o["amount"] for o in orders), 2),
        "firstOrder": first_order,
        "lastOrder": last_order,
        "storeCount": len(stores),
        "monthSpan": month_span,
    }


def build_export(raw_orders):
    """原始订单列表 → 完整导出文档（网页按这个结构解析）。"""
    orders = []
    for raw in raw_orders:
        o = normalize_order(raw)
        if o is not None:
            orders.append(o)
    orders.sort(key=lambda o: o["createTime"], reverse=True)
    return {
        "exportedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "McDonald's China MCP · order-list",
        "currency": "CNY",
        "orders": orders,
        "summary": build_summary(orders),
    }


# ---------------------------------------------------------------------------
# 3. 拉取
# ---------------------------------------------------------------------------
def fetch_orders(page_size, max_pages, verbose=True):
    """分页调用 order-list 直到没有更多，返回原始订单列表。"""
    raw_orders = []
    for page in range(1, max_pages + 1):
        env = tool_raw("order-list", {"pageSize": page_size, "pageIndex": page})
        err = _error_text(env)
        if err:
            raise RuntimeError("order-list 返回错误：%s" % err)
        data = _result(env) or {}
        if not isinstance(data, dict):
            data = {}
        chunk = (data.get("data") or {}).get("list")
        if chunk is None:
            chunk = data.get("list")
        if not isinstance(chunk, list) or not chunk:
            if verbose:
                print("    page %-2d → 0 条，结束翻页" % page)
            break
        raw_orders.extend(chunk)
        if verbose:
            print("    page %-2d → %d 条（累计 %d）" % (page, len(chunk), len(raw_orders)))
        if len(chunk) < page_size:
            break
    return raw_orders


# ---------------------------------------------------------------------------
# 4. 终端报告
# ---------------------------------------------------------------------------
def print_report(doc, out_path):
    s = doc["summary"]
    orders = doc["orders"]
    print("")
    print("=" * 66)
    print("订单导出完成 → %s" % out_path)
    print("=" * 66)
    print("  订单数      %d 单" % s["orderCount"])
    print("  总消费      ¥%.2f" % s["totalAmount"])
    print("  时间跨度    %s → %s（%d 个月）" % (s["firstOrder"] or "-",
                                                s["lastOrder"] or "-", s["monthSpan"]))
    print("  门店数      %d 家" % s["storeCount"])
    stores = {}
    for o in orders:
        if o["storeName"]:
            stores[o["storeName"]] = stores.get(o["storeName"], 0) + 1
    if stores:
        print("  常去门店：")
        for nm, n in sorted(stores.items(), key=lambda kv: (-kv[1], kv[0]))[:3]:
            print("    %-2d 单  %s" % (n, nm))
    counts = count_items(orders)
    if counts:
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        print("  TOP5 餐品：")
        for nm, n in top:
            print("    %-2d 次  %s" % (n, nm))
    zero = [o for o in orders if o["amount"] == 0]
    if zero:
        print("  提示：有 %d 单金额为 0（积分兑换 / 活动赠送 / 员工餐），已按真实金额计入。"
              % len(zero))
    print("")
    print("  下一步：打开 index.html → 生成套餐 → 点「导入我的订单」选择这个文件。")
    print("")


# ---------------------------------------------------------------------------
# 5. main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="用麦当劳 MCP 拉取当前用户历史订单，导出网页可导入的 my-orders.json")
    ap.add_argument("--out", default="my-orders.json", help="输出文件路径（默认 my-orders.json）")
    ap.add_argument("--page-size", type=int, default=50, help="每页条数（默认 50）")
    ap.add_argument("--max-pages", type=int, default=20, help="最多翻页数（默认 20）")
    args = ap.parse_args()

    if not TOKEN:
        print("")
        print("!! 未设置 MCD_MCP_TOKEN，无法读取订单。")
        print("")
        print("这个脚本需要麦当劳 MCP 的访问 token（从你的 MCP 客户端配置里取）：")
        print("")
        print("  macOS / Linux      export MCD_MCP_TOKEN=你的token")
        print("  Windows PowerShell $env:MCD_MCP_TOKEN=\"你的token\"")
        print("  Windows CMD        set MCD_MCP_TOKEN=你的token")
        print("")
        print("然后重新运行：python export_orders.py")
        print("")
        print("token 只存在于环境变量里，本脚本不会把它写进任何文件。")
        print("")
        return 2

    print("=" * 66)
    print("麦当劳 MCP 订单导出")
    print("  endpoint  %s" % ENDPOINT)
    print("=" * 66)

    try:
        # 握手：202 空响应属正常，不解析结果
        rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "mcd-name-meal", "version": "1.0"}})
        print("[1] initialize            → 握手完成")
        raw_orders = fetch_orders(args.page_size, args.max_pages)
    except RuntimeError as e:
        print("")
        print("!! 拉取失败：%s" % e)
        print("   常见原因：token 过期 / 无 order-list 权限 / 网络不通。")
        print("   token 请在 MCP 客户端里重新获取后再试。")
        print("")
        return 1
    except KeyboardInterrupt:
        print("\n!! 已中断，未写出任何文件。")
        return 130

    print("[2] order-list            → 共 %d 条原始订单" % len(raw_orders))

    doc = build_export(raw_orders)
    if not doc["orders"]:
        print("")
        print("!! 没有拉到可用的历史订单（可能是账号没有订单，或全部为已取消订单）。")
        print("   未写出文件。")
        print("")
        return 1

    out_path = os.path.abspath(args.out)
    parent = os.path.dirname(out_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print_report(doc, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
