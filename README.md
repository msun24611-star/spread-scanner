# 📐 Spread Scanner —— 垂直价差机会发现网站

实时扫描美股期权链，筛选并按**综合评分**排序**信用价差**（Bull Put / Bear Call，卖方）机会。
仿 OSM.AI 思路自建，个人研究用。数据来自老虎 Open API。

## 启动

```bash
# 1) 装依赖
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) 配置凭证:复制 .env.example 为 .env 并填入你自己的老虎 TIGER_ID / 账户号 / 私钥路径
#    (或直接设为系统环境变量)

# 3) 启动
.venv\Scripts\python.exe app.py
# 浏览器打开 http://localhost:3100
```

> 凭证(TIGER_ID / 账户号 / 私钥)全部从环境变量读取,不写在代码里。见 `.env.example`。

## 前置：行情权限

需要老虎 Open API 的美股期权行情权限（**独立于 App/PC，需单独购买**）：
- `usOptionQuote` —— 美股期权行情 OPRA（拉期权链 + greeks）
- `usQuoteBasic` / `usStockQuote` —— 美股标的现价

购买路径：**Tiger Trade App → 我的 → 行情权限 → OpenAPI权限**。
打开网页顶部横幅会自动自检权限是否到位；也可直接看 `GET /api/status`。

⚠️ **多设备抢占**：`QuoteClient` 默认会抢占行情权限，同一时刻只允许一台设备取数。
如果同时开着 tiger-mcp 或 App 拉行情，会互相挤。用扫描器时尽量别让其他设备同时抢。

## 结构

| 文件 | 作用 |
|------|------|
| `app.py` | Flask 服务：接口 + 静态托管 + 内存缓存(TTL 5min)，端口 3100 |
| `tiger.py` | 老虎只读行情客户端(凭证从环境变量读) |
| `screener.py` | 核心：拉链→枚举信用价差→算指标→综合评分排序 |
| `watchlist.json` | 精选流动性池(可编辑，增删标的改这里) |
| `public/index.html` | 单页前端：扫描全池/单标的、过滤器、机会卡片 |

## 接口

- `GET /api/status` —— 权限自检
- `GET /api/watchlist` —— 精选池
- `GET /api/scan` —— 扫全池，全局排序
- `GET /api/scan?ticker=NVDA` —— 扫单个标的
- 可选参数：`min_dte,max_dte,min_ror,min_pop,min_oi,short_delta_min,short_delta_max`
  （例：`/api/scan?ticker=NVDA&min_dte=30&max_dte=45&min_pop=0.7`）

## 指标口径

- **credit（净收）**：卖短腿吃 bid、买长腿付 ask（保守）；另存 `mid_credit` 中价版本。
- **max_loss（最大亏损）**：`宽度 − credit`。
- **RoR（回报风险比）**：`credit / max_loss`。
- **POP（胜率）**：`1 − |短腿 delta|`；期权链没返回 delta 时用 Black-Scholes 从 IV 兜底算。
- **综合评分**：`0.35·POP + 0.35·RoR + 0.15·IV + 0.15·流动性`，各项在当前结果集内归一化，×100。

## 默认筛选参数（screener.py DEFAULTS）

DTE **1–7 天**(短线)、短腿 |Δ| 0.15–0.35、最小 RoR 15%、最小 POP 60%、短腿 OI≥50、买卖价差≤30%。

## 财报标记 & 七大科技股页

- **财报标记**:每条价差若持仓期(今天~到期日)内撞财报,标 ⚠️(数据来自 `get_corporate_earnings_calendar`,全市场缓存 1 小时)。前端可勾「隐藏撞财报」。短 DTE 尤其要避财报。
- **七大科技股高频到期页** `/tech.html`(接口 `/api/tech-cadence`):列 AAPL/MSFT/GOOGL/AMZN/NVDA/META/TSLA 近两周到期节奏,标出「每周一/三/五」这种高频到期(适合 1–7 天短线),点「扫 1–7 天机会」跳到主页自动扫该票。

## v1 范围 & 后续可扩展

v1 只做信用价差。代码已预留 `side` 字段，后续可加：
- 借方价差（Bull Call / Bear Put）
- 铁鹰/铁蝶合成
- 真正的 IV Rank（接历史 IV 数据源；当前用短腿 IV 水平近似）
- 定时快照 + 历史归档

## 合规

个人研究工具，不代客下单、不构成投资建议。数据是否实时取决于行情权限。
私钥、账户号等敏感信息一律走环境变量，不入库；`.env` / `*.pem` 已在 `.gitignore` 中排除，切勿外传。
