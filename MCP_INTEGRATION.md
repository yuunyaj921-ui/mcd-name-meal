# MCP 集成说明

本文记录本项目与**麦当劳中国 MCP** 的真实集成方式、调用链、实测协议行为与业务价值。所有内容来自 2026-10-22 的真实调用，未使用任何模拟响应。

---

## 1. MCP Server

| 项 | 值 |
|---|---|
| Server | 麦当劳中国 MCP |
| Endpoint | `https://mcp.mcd.cn` |
| 传输 | Streamable HTTP（JSON-RPC 2.0 over POST） |
| 认证 | `Authorization: Bearer ${MCD_MCP_TOKEN}` |
| 工具总数 | **35**（`tools/list` 实测） |
| 开放平台 | https://open.mcd.cn/mcp |

配置示例见 [`mcp-config.example.json`](mcp-config.example.json)，仅使用环境变量占位符，仓库内不含任何真实 Token。

---

## 2. 使用的 Tool（5 个）

| Tool | 作用 | 本项目用途 | 副作用 |
|---|---|---|---|
| `tools/list` | 发现工具与参数契约 | 确认 35 个工具及参数名（避免猜错参数） | 无 |
| `query-meals` | 门店在售餐品列表 | 拉取当前门店 14 个分类 / 86 个在售餐品 | 无 |
| `query-meal-detail` | 餐品详情与套餐构成 | 把 `code` 解析成餐品名称 | 无 |
| `calculate-price` | 按门店算价（含优惠） | 取每项餐品的门店实时价格 | 无（仅算价，不建单） |
| `list-nutrition-foods` | 官方营养数据 | 取每项餐品的热量（`energyKcal`）等 6 项指标 | 无 |

**安全边界**：本项目**不调用**任何写操作工具——不使用 `create-order`、`cancel-order`、`mall-create-order`、`party-order-create`、`draw-lottery`、`auto-bind-coupons` 等。`refresh_data.py` 在代码中把可调用工具限定为上述 5 个只读工具。

---

## 3. 调用链

```
用户/开发者执行  python refresh_data.py
        │
        ├─ 1. initialize                 → 握手（HTTP 200）
        ├─ 2. tools/list                 → 35 个工具 + inputSchema
        ├─ 3. query-meals(storeCode, beType=1, orderType=1)
        │                                 → 14 分类 / 86 个在售餐品（code + tags）
        ├─ 4. query-meal-detail(storeCode, code, beType=1, orderType=1)
        │                                 → 餐品名称（逐个解析，86 次）
        ├─ 5. calculate-price(storeCode, beType=1, orderType=1,
        │                     items=[{productCode, quantity:1}])
        │                                 → 单项门店实时价格（单位：分）
        └─ 6. list-nutrition-foods()      → 160 条官方营养记录
                    │
                    ▼
        data/menu.real.json  ←  名称 + 实时价格 + 热量 合并
        data/menu.js         ←  可直接替换 index.html 中 MENU 常量的数据块
```

### 页面为什么不在运行时调 MCP

`index.html` 是**零依赖单文件、断网可用、无 Token** 的设计，这是为了把体验门槛降到最低（打开就能玩，不需要注册开放平台）。

MCP 的真实使用发生在**数据层**：`refresh_data.py` 通过 MCP 拉取菜单、价格与营养，生成页面所需的数据快照。页面内嵌的就是这份由 MCP 产出的数据。

因此：
- **MCP 是数据来源**（可复核、可重跑、可更新），不是运行时依赖
- 任何人执行 `refresh_data.py` 都能拿到同一套结构的真实数据
- 快照时间与门店编码在页面底部与 README 中都明确标注

---

## 4. 实测协议行为（踩过的坑，供其他参赛者参考）

以下均为真实调用中发现，不是文档抄录：

### 4.1 响应形态有三种

| 情况 | 表现 | 处理 |
|---|---|---|
| SSE | 响应体带 `data: {...}` 前缀 | 先剥 `data:` 再 parse |
| 纯 JSON | 直接是 JSON | 直接 parse |
| `initialize` | 可能返回 HTTP 202 空响应 | 空响应不视为失败，继续下一步 |

### 4.2 会话是无状态的

不需要维护 `mcp-session-id`。`initialize → notifications/initialized → tools/call` 每个 POST 相互独立，可并发（本项目为保守起见串行 + 间隔 120ms）。

### 4.3 业务数据有两个可能位置

优先读 `result.structuredContent`；没有时从 `result.content[0].text` 里提取内嵌 JSON——注意这个字段是「给 LLM 看的说明文字 + JSON」的混排文本，需要正则提取。

### 4.4 参数名与单位陷阱（最耗时的部分）

| 陷阱 | 现象 | 正确做法 |
|---|---|---|
| `query-meal-detail` 的餐品参数名 | 传 `mealCode` / `productCode` 都报「**code 不得为空白**」 | 参数名就是 **`code`** |
| `calculate-price` 的商品列表参数名 | 传 `products` / `productList` 都报「检查必填参数」 | 参数名是 **`items`**，元素字段是 `productCode` + `quantity` |
| `query-meals` 必填参数 | 空参报「缺少参数」 | 必传 `storeCode` + `beType` + `orderType` |
| **单位不一致** | `calculate-price` 返回 `2350`，`query-meals`/快照是 `23.5` | `calculate-price` 是**分**，其余是**元**；内部统一按分换算 |
| `list-nutrition-foods` 返回格式 | 不是 JSON 对象数组 | 是 `[160]{col,col,...}:` 开头的**表格文本**，需按列名解析 |

---

## 5. 真实响应样本（脱敏）

### 5.1 `calculate-price` — 单项算价

请求：

```json
{
  "storeCode": "3450314",
  "beType": 1,
  "orderType": 1,
  "items": [{ "productCode": "1406", "quantity": 1 }]
}
```

响应（节选）：

```json
{
  "success": true,
  "code": 200,
  "data": {
    "productOriginalPrice": 2350,
    "productPrice": 2350,
    "discount": 0,
    "price": 2350,
    "productList": [
      { "productCode": "1406", "productName": "板烧鸡腿堡", "quantity": 1, "subtotal": 2350 }
    ]
  }
}
```

> `2350` 分 = ¥23.5，即该门店当日板烧鸡腿堡的真实售价。

### 5.2 `list-nutrition-foods` — 营养表（表格文本格式）

```
[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
  猪柳麦满分,null,1288,308,16,16,24,781,213
  猪柳蛋麦满分,null,1618,387,23,21,25,846,243
  中薯条,null,1210,289,4,12,38,165,18
  大薯条,null,1587,379,6,16,50,216,23
```

### 5.3 `query-meals` — 门店菜单结构

```json
{
  "success": true,
  "data": {
    "categories": [
      { "name": "人气热卖", "meals": [{ "code": "9900016076", "tags": ["心形薯饼同款比心"] }] },
      { "name": "鸡肉汉堡/卷", "meals": [{ "code": "9900016075" }] }
    ]
  }
}
```

---

## 6. 业务价值

### 6.1 对用户

传统「输入名字抽套餐」只能编造菜单。本项目让**每一份套餐里的每个数字都可追溯**：

- 餐品名 → 门店当前在售的真实商品
- 价格 → `calculate-price` 算出的门店实价（不同门店、不同时段可能不同）
- 热量 → 官方营养表的 `energyKcal`

所以当两个人互相比较「你是什么套餐」时，比的是**真实商品与真实价格**，而不是随机文案。

### 6.2 对 MCP 生态

- 本项目沉淀了一份**可复现的调用脚本**（`refresh_data.py`），把 5 个工具的完整调用链、参数契约、单位换算、返回解析都写成了可运行代码，可直接被其他开发者复用
- 第 4 节记录的 5 个实测陷阱（参数名 `code` / `items`、单位分与元、表格文本格式）在官方文档里没有显式说明，属于实测才能发现的行为

### 6.3 数据可更新性

门店菜单会变（本店 `query-meals` 返回 86 个在售餐品，其中不少是套餐组合而非单品）。`refresh_data.py` 的设计意图是**让数据可刷新而非写死**：

```bash
export MCD_MCP_TOKEN=你的token
python refresh_data.py --store 3450314        # 生成 data/menu.real.json
python refresh_data.py --store 3450314 --write # 同时回写 index.html 的 MENU 常量
```

---

## 7. 已知限制（如实说明）

### 7.1 价格数据的范围

**已核实（11 项）**：2026-10-22 通过 `calculate-price` 逐项查询**麦当劳成都府青路(恒大)餐厅**（门店编码 3450314）得到的实价，例如巨无霸 2650 分 = ¥26.5、板烧鸡腿堡 2350 分 = ¥23.5。

**这是单店价格，不是全国统一价。** 同一商品在不同城市、不同门店可能不同。

**未核实（7 项）**：大薯条、小薯条、雪碧中杯、鲜萃咖啡大杯、无糖可乐中杯、奥利奥麦旋风、菠萝派。

这 7 项在该门店（以及同城另外两家门店 3450233、3450330）都没有同规格单品：

| 商品 | 实际情况 |
|---|---|
| 大薯条 / 小薯条 / 无糖可乐中杯 | 三家门店均无单品在售 |
| 雪碧中杯 / 鲜萃咖啡大杯 | 只有**小杯**规格，不可跨规格套用 |
| 菠萝派 | 当日只有「玫瑰红糖糍粑风味派」，非同一商品 |
| 奥利奥麦旋风 | 在售为「经典麦旋风」，非同一商品 |

处理方式：**保留价格字段仅用于展示，商品后加 `*`，含此类商品的合计以「≈」标示为约数**。不把它们包装成"参考价"，也不用于精确金额计算。

### 7.2 其他限制

1. **菜单池为稳定快照**：`index.html` 内嵌的 19 项餐品（主食 5 / 小食 5 / 饮料 4 / 甜品 4，共 400 种组合）。门店实时菜单以套餐组合为主、单品命名不统一，直接用于四分类哈希池会导致组合不稳定，因此保留固定池子。
2. **`refresh_data.py` 无法 100% 匹配全部 19 项**：实测某门店只能稳定匹配到 8 项（如「香芋派」在菜单里可能以通用名「派」出现）。脚本会把未匹配项明确列出，不静默填充。
3. **价格与供应随时段变化**：以上价格为 2026-10-22 查询结果，活动、时段、门店均可能影响实际售价。
4. **营养成分与价格相互独立**：营养成分来自 `list-nutrition-foods`（160 条），未核实价格的商品其营养数据仍然有效，因此成分表不受价格核验状态影响。
