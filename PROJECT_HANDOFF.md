# GJDJK（A股国家队宽基ETF动向监控）开发交接、架构规格与演进日志

> **本文档用途**：面向后续接手维护的开发者、AI Agent 或换机迁移时的本人。脱离历史对话上下文后，仅凭本文件与代码库即可完整理解本项目的设计红线、量化算法数学细节、外部数据源特性、并发架构、历史实证结果及日常运维方式。
>
> **最后更新**：2026-11-27（当前版本：**v3.1.1**，回归测试 **15/15 通过**，实盘核验 **249 个交易日**）

---

## 1. 产品定位与不可违背的设计红线

本项目是面向 A 股个人投资者的 **Win11 本地化「国家队（中央汇金、国新投资、中国诚通等）宽基 ETF 资金动向监控」桌面应用**，完全离线运行于本机，不依赖任何云服务器或外部数据库。

**后续任何开发或重构，必须严守以下 6 条用户明确确认的产品红线**：

1. **零后台常驻进程（0 MB 空闲内存）**
   - 不使用系统托盘驻留（已彻底移除 `pystray` / `APScheduler` 常驻方案）。
   - **定时巡检模式**（`pythonw.exe app.py --scheduled-check`）：由 Win11 原生任务计划程序（`GJDJK_DailyCheck`，免管理员权限）在每晚 **22:30** 交易所份额公布后唤醒执行，并开启 `StartWhenAvailable=True`（当晚关机错过时，下次开机自动补跑）；巡检完几秒即彻底退出，内存归零。
   - **看板浏览模式**（`pythonw.exe app.py`）：按需双击快捷方式打开独立浅色 `pywebview` 桌面窗口；点击右上角 `×` 关窗时优雅等待最多 `2.5s` 落盘后直接彻底退出（`os._exit(0)`）。
2. **大号、置顶、必须手动关闭的提醒卡片**
   - 仅当出现 **已核实（`VERIFIED`）的重大异动**、**新官宣公告** 或 **数据源故障预警（`HEALTH_ALERT`）** 时弹出 `600px` 宽的独立浅色高对比窗口（[src/alert_dialog.py](src/alert_dialog.py)），绝不自动消失，必须手动点击“我知道了”或“打开完整看板”。
   - **绝不对未核实的盘面猜想（`MARKET_PRELIM` / `SHARES_PARTIAL`）弹异动窗**。
3. **个人复盘批注与标签永久保留**
   - 用户的个人笔记与标签单独持久化于 `user_notes` 表，任何全量重算、数据库升级迁移或窗口日期裁剪均不得删除或覆盖（即便只填了标签、正文为空也必须完整保留）。
4. **单机回环实用主义，避免过度企业级防御**
   - FastAPI 严格绑定 `127.0.0.1` 仅本机使用，**不需要**引入登录 Token 或复杂鉴权中间件；但必须保留强类型 Pydantic 校验、Webhook URL 协议白名单（允许 `http://` 局域网与 `https://`）、前端 `escapeHtml()` 防 XSS 与 CSV 文本列防 Excel 公式注入。
5. **Windows 系统代理兼容性**
   - 本机可能存在拦截 `127.0.0.1` 或部分境内金融接口的系统代理，因此本地探活与外部抓取均通过 [src/fetchers/http_client.py](src/fetchers/http_client.py) 实现 `trust_env=False` 直连优先、失败再降级走系统代理。
6. **PowerShell 5.1 兼容注意**
   - 开发机默认 Shell 为 Windows PowerShell 5.1（不支持 `&&` 链式符，命令行多行脚本建议通过 `@' ... '@ | .\.venv\Scripts\python.exe -` 执行）。

---

## 2. 项目目录与核心模块全景

```text
d:\GJDJK\
├── app.py                     # 主入口：桌面窗口 / --scheduled-check / --boot-check / --sync-only / --server-only
├── config/
│   └── watchlist.yaml         # 31只宽基ETF（含12只 huijin_core 标记）、8大宽基组、8大基准指数、国家队实体词典与静态兜底阈值
├── data/                      # [Git忽略] 本地持久化目录
│   ├── gjdjk.db               # SQLite WAL 主库（全部行情、份额、日志、底牌、公告、笔记、Outbox）
│   ├── gjdjk.ico              # 自动生成的桌面图标
│   └── logs/                  # app.log / alert.log / stdio.log / crash.log
├── src/
│   ├── config.py              # 路径常量、DEFAULT_SETTINGS 与 YAML 配置加载
│   ├── db.py                  # SQLite 数据访问层、幂等字段/表迁移、跨进程租约、Outbox 认领、原子发布
│   ├── analyzer.py            # 核心量化引擎 v3.1（核心池门控、时点一致锚点、鸽笼底线、双锚标尺、五维信号灯）
│   ├── sync_service.py        # 智能同步状态机、4月/9月底牌自动刷新器、健康自检与故障预警分发
│   ├── server.py              # FastAPI 本地接口（Pydantic 严格校验、看板聚合、CSV/Markdown 导出）
│   ├── notifier.py            # 5 通道通知分发（Win11大卡片、飞书、企微、Server酱/PushPlus、自定义Webhook）
│   ├── alert_dialog.py        # 独立进程大号置顶提醒卡片（支持买/卖/调仓/公告/橙红数据源故障预警）
│   ├── autostart.py           # Win11 任务计划程序管理（GJDJK_DailyCheck，默认22:30 + StartWhenAvailable）
│   ├── window.py              # Edge --app 降级窗口启动器
│   ├── icon_gen.py            # 本地矢量 ICO 图标生成
│   └── fetchers/
│       ├── http_client.py     # 代理感知容错 HTTP 客户端（直连优先 → 系统代理回退）
│       ├── market.py          # 新浪/腾讯双源日线行情、估算成交额标记、腾讯5分钟K线完整日尾盘提取
│       ├── shares.py          # 上交所/深交所官方日频 ETF 份额（含无日期深交所快照拒绝守卫）
│       └── holdings_ann.py    # 东财多报告期前十大持有人（含法定可用日）+ 巨潮公告抓取与事件分类
├── web/
│   ├── index.html             # 单页浅色仪表盘（今日全景 / 每日日志 / 公告与底牌 / 提醒与设置）
│   ├── app.js                 # 前端交互、ECharts 双轴图表、跨零区间独立着色、健康异常横幅
│   ├── styles.css             # Win11 Fluent 浅色高对比样式
│   └── vendor/
│       └── echarts.min.js     # 本地离线 ECharts（1.03 MB，无外网 CDN 依赖）
├── tests/
│   └── test_core.py           # 15 项全量回归测试（覆盖全部核心算法、P0/P1 修复点与健康监控）
├── 启动国家队监控.vbs          # 无黑框启动桌面窗口（调用 .venv\Scripts\pythonw.exe app.py）
├── 开启晚间自动巡检.bat        # 一键注册 Win11 每日 22:30 定时巡检任务（调用 set_autostart(True, '22:30')）
├── 关闭自动巡检.bat            # 一键卸载定时巡检任务（调用 set_autostart(False)）
├── LICENSE                    # MIT 开源协议
├── requirements.txt           # 精简后的 Python 依赖清单
├── README.md                  # 项目简介、实盘校核与快速使用说明
└── PROJECT_HANDOFF.md         # 本文档
```

---

## 3. 量化分析引擎 v3.1 完整数学规格（`src/analyzer.py`）

### 3.1 严格分层监控池（`config/watchlist.yaml`）
- **31 只全宽基池**：覆盖 `HS300`、`SZ50`、`ZZ500`、`ZZ1000`、`A500`、`CYB`、`KC50`、`SOE` 8 大宽基方向。
- **严格 12 只中央汇金核心池（`huijin_core: true`）**：
  - 沪深300四巨头：`510300`、`510310`、`510330`、`159919`
  - 上证50/180：`510050`、`510180`
  - 中证500：`510500`、`512500`
  - 中证1000四巨头：`512100`、`159845`、`560010`、`159629`
  - **关键纪律**：`159915`（易方达创业板ETF，虽汇金持有 54% 但混合资金占比高）与 `560170`（国新央企科技，主体为国新投资而非汇金）**绝不混入 `huijin_core` 门控池**，避免普通市场申购创业板时误报国家队强护盘。

### 3.2 数据完备性与拆合份防误判
- **拆合份识别**：当单日份额变动超过 `35%` 且 `总份额 × 收盘价` 总规模变化 `< 8%` 时，判定为折算/拆合份，显式标记 `flow_status = 'split'` 并将 `share_delta`、`net_flow_yi` 写为 `NULL`（杜绝千亿级假净买入）。
- **严格 `VERIFIED` 门控**：同时满足 **12 只汇金核心池全部完备（`flow_status in ('ok', 'split')` 且非无日期快照）** + **上交所覆盖率 ≥ 90%** + **深交所覆盖率 ≥ 90%** 才标记为 `VERIFIED`；否则降级为 `SHARES_PARTIAL` 或 `MARKET_PRELIM`，并在 `daily_logs.meta_json` 记录 `completeness_pct` 与 `missing_core_codes`。

### 3.3 时点一致（Point-in-Time）底牌锚点 + 时效周转率衰减 + 鸽笼底线
1. **零未来函数查询**：每期定期报告绑定法定最晚可用日 `published_at`（年报 `12-31 → 次年 03-31`，半年报 `06-30 → 当年 08-31`）。计算历史交易日 `trade_date` 时，仅允许使用 `published_at <= trade_date` 的最新一期底牌，取消旧版 15% 硬底线（如 `510100` 保持真实 `5.83%`）。
2. **老化与换手衰减**：
   - `freshness = clamp(exp(-age_days / 240) * exp(-0.45 * min(turnover_ratio, 4.0)), 0.25, 1.0)`
   - 随报告老化与累计换手率上升，有效权重平滑向结构先验收缩，估算区间 `[lo_f, hi_f]` 同步展宽，弱证据情景下限允许归零（`0.0`）。
3. **单边大卖「鸽笼库存底线」（v3.1 新增，零未来函数）**：
   - 对单边净赎回日（`share_delta < 0`），维护自锚点报告期至前一日的区间最低总份额 `prev_min_s`，令有效上限 `eff_cap = min(H_0, prev_min_s)`（`H_0` 为报告期实名持仓份数）。
   - 仅当当日总份额 `S_now < eff_cap`（砸穿实名底牌新低）时，由恒等式 $H_t \le S_t$ 可知击穿部分 `forced_sh = min(|dS|, eff_cap - S_now)` 数学上必然来自国家队卖出，按 `forced_frac = forced_sh / |dS|` 托起下限 `lo_f` 和上限 `hi_f`，消除高周转衰减导致的单边大卖低估。
   - 反弹后的普通卖出（`S_now >= prev_min_s`）与买入波段（`share_delta > 0`）均不触发。

### 3.4 升级版双锚自适应百分比标尺（`_compute_adaptive_thresholds`）
- **锚一（规模锚）**：按当日核心 12 只与全池总规模（AUM）的固定比例伸缩（核心强线 `0.95%`、全池强线 `0.80%`）。
- **锚二（比率分位锚）**：对近 120 交易日的 `|净流| / 当日 AUM` 比率求 55%/80% 分位，再映射回今日 AUM，解决跨规模周期（万亿缩水/膨胀）下旧版原始金额分位触顶问题。
- **平滑热身**：有效历史样本 15→60 日期间，分位锚权重 `w_hist = clamp((n_hist - 15) / 45, 0.0, 0.50)` 线性渐增，彻底消除冷启动第 25 日断崖跳变；每日标尺随日志持久化于 `daily_logs.scale_json`。

### 3.5 五维信号灯与定性结论
- 五维权重：`汇金核心池净申赎 (36%)`、`头部同步共振 (22%)`、`全天量能热度 (16%，基于前20日中位数)`、`尾盘30分脉冲 (10%，DB 5日基线金额加权，仅接受>=44根完整交易日)`、`60日价格区间位置 (16%)`；缺项（如历史日无 5 分钟线）自动重归一化。
- **组内共振**：要求同方向且至少 2 只巨无霸（单巨头组如创业板绝不自共振，反向 ETF 不沾光）。
- **调仓与净方向解耦**：只要双向流入/流出均达标即写入 `rotation_desc`（含 `匹配对冲约 X亿`）；仅当净额接近中性（`|total_net| <= 0.30*(in+out)` 且核心与全池均未破强线）时主结论判为 `ROTATION`，强净买/净卖日保留买/卖主结论并叠加调仓标注。
- **无份额盘面初判保护（v3.1）**：仅低位/下跌放量初判 `PRELIM_RESCUE`、高位（≥75%）上涨放量初判 `PRELIM_SELL`；低位利好暴涨放量（如 `2026-11-27` 60日区间 23%、涨 `+4.17%`）保持中性 `PRELIM_WATCH（盘面显著放量 · 待份额核实）`，绝不盲猜派发。

---

## 4. 外部数据源矩阵与故障运维手册

| 数据项 | 主/备接口实现 | 对应文件 | 常见风险与修复方式 |
|---|---|---|---|
| **8大基准 & 31只ETF日线** | 主：新浪纯 JSON (`CN_MarketDataService`)<br>备：腾讯前复权日线 (`ifzq.gtimg.cn`) | [src/fetchers/market.py](src/fetchers/market.py) | 极稳定；窗口首日 `pct_chg=None` 由 `analyzer.py` 用库内前收补算；腾讯缺成交额时以 `量×均价` 估算并置 `amount_is_estimated=1`。 |
| **近 5~6 日 5 分钟 K 线** | 腾讯 `mkline?param=...,m5,,320` | [src/fetchers/market.py](src/fetchers/market.py) | 强约束 `bars>=44 && min_hhmm<='0945' && max_hhmm>='1455'`，自动剔除半日与截断首日。 |
| **上交所日频 ETF 份额** | `ak.fund_etf_scale_sse(date=YYYYMMDD)` | [src/fetchers/shares.py](src/fetchers/shares.py) | 依赖 `akshare`；通常盘后 `19:30~20:30` 更新。若连续缺数，优先执行 `.venv\Scripts\pip install -U akshare` 并检查返回列名。 |
| **深交所日频 ETF 份额** | `ak.fund_etf_hist_szse`（45天分片）<br>安全兜底：`ak.fund_etf_scale_szse()`（仅含匹配日期列才采纳） | [src/fetchers/shares.py](src/fetchers/shares.py) | 依赖 `akshare`；通常 `20:00~21:00` 更新。无明确日期列的旧快照已被硬拒绝，绝不污染历史日。 |
| **前十大持有人底牌** | 东财 `datacenter-web.eastmoney.com` `RPT_F10_FUND_HOLDERTOP10`（`pageSize=35`，保留近3期） | [src/fetchers/holdings_ann.py](src/fetchers/holdings_ann.py) | **已知现象**：截至 2026-11-27 东财该表最新停在 `2025-12-31`（未同步 `2026-06-30`）。系统在 4月初/9月初后每 3 天自动重试；若长期滞后可考虑新增巨潮/季报备用源。 |
| **国家队官宣公告** | 巨潮资讯 `cninfo.com.cn/new/hisAnnouncement/query` | [src/fetchers/holdings_ann.py](src/fetchers/holdings_ann.py) | 自动按标题分类为 `ETF/基金增持`、`主体股权/要约事件`、`公开增持/表态`，过滤债券/同名噪音。 |

**自动化运维与自检机制（`src/sync_service.py`）**：
- **4 月初年报 / 9 月初半年报自动刷新**：`expected_latest_report_date()` 自动比对库内最新 `report_date`，一旦落后法定披露期，每 3 天自动重试抓取（哪怕当日行情已最新）；新报告期一入库立即触发全量历史重算。
- **数据源异常自动预警**：`evaluate_system_health()` 监测到 **连续 ≥2 次同步报错** 或 **连续 ≥2 个已完成交易日未核实（`SHARES_PARTIAL`/`MARKET_PRELIM`）** 时，自动定位上交所/深交所缺口与缺失核心代码，弹出醒目橙红色 `HEALTH_ALERT` 大卡片（经 Outbox 每日最多 1 次防刷屏）并在看板顶部亮起告警横幅。

---

## 5. 数据库 Schema 与并发控制（`src/db.py`）

SQLite 开启 `WAL`、`foreign_keys=ON`、`busy_timeout=30000`：
- **核心业务表**：`benchmark_daily`、`etf_daily`（原始行与派生列分离写入）、`daily_logs`（含 `scale_json`、`meta_json`）、`etf_holdings`（含 `published_at`）、`announcements`（含 `event_category`）。
- **高可靠运维表**：
  - `user_notes`：独立存储 `(trade_date, user_note, user_tags)`，永不随裁剪丢失。
  - `notification_outbox`：主键 `(dedup_key, channel)`，通过 `BEGIN IMMEDIATE` 原子认领发送权；仅成功通道记 `sent`，失败通道记 `failed` 下次自动重试（已幂等迁移历史 `notifications_sent`）。
  - `sync_leases`：跨进程同步互斥租约，带 Windows `OpenProcess` PID 存活检测，防双开并发冲突。
  - `sync_runs`：同步运行审计日志，启动时自动把上次异常中断的 `running` 标记为 `interrupted`。
  - `app_settings`：JSON 键值配置表。

---

## 6. 实盘 249 日历史校核成绩单（`2025-11-19 ~ 2026-11-27`）

在 v3.1 算法下，对全部 249 个交易日进行了多维度外部实证校核：

1. **底层资金流对标权威 Wind 统计（2026-09-01 ~ 11-26，57 天，9 大核心宽基）**：
   - 累计净申购：**`+6,759.6 亿`** vs 媒体公开统计 **`6,759.6 亿`**（误差 **`0.0 亿`**）
   - 单日峰值（`2026-11-20`）：**`+895.7 亿`** vs 媒体公开统计 **`895.7 亿`**（误差 **`0.0 亿`**）
   - `510300` 2026Q1 份额降至 **`448.3亿份`**（减 `440.0亿份`）、`2026-06-30` 半年报总份额 **`189.1亿份`**（官方 `189.15亿份`），全线精准吻合。
2. **四大宏观波段实证校核**：
   - **2025-11~12 抗误报压力测试**：全池因中证 A500 净申购 `+961.5亿` 达 `+1,054.7亿`，实际 2025 年报显示汇金基本未动；算法判定 **0 天强买、0 天强卖**，完美抗噪音。
   - **2026 上半年（1~6月）万亿大减仓**：权威半年报测算真实减持 **`1.3~1.4 万亿`**（12 核心池鸽笼硬下限至少 `11,555亿`）；算法精准捕捉 1 月降温（连续 13 日全线报警）与 4~6 月二波降温，加入鸽笼底线后累计估算 **`[-15,690, -13,098, -10,506] 亿`**（中值 **`-13,098 亿`** 正中靶心）。
   - **2026-07~09 震荡转折**：准确过滤 `2026-07-28`（核心池 `-18.6亿`、纯创业板+科创拉动）不再误报国家队大调仓，准确识别 9 月转正拐点。
   - **2026-10~11 秋冬季大托底**：36 个核实日抓出 18 天强买 + 7 天温买，估算 **`[+2,139, +3,800, +5,461] 亿`**（中值 `+3,800亿`），与 2026-11-27 国常会“加力发挥国家队稳市作用、优化中央汇金类平准基金功能”定调一致。
3. **逆周期行为与后验走势统计**：
   - 买入信号日大盘下跌占比 **61.9%**（均跌 `-0.40%`），卖出信号日大盘上涨占比 **55.3%**（均涨 `+0.13%`）。
   - 实战提醒：`STRONG_BUY` 是左侧托底信号（+20日胜率 35%），**不可当成次日必涨的短线抄底指令**；`STRONG_SELL` 降温预警的右侧压制效应很强（+20日均跌 `-1.99%`、胜率仅 26%）。

---

## 7. 版本演进简要日志

- **v1.0**：初版基础框架（FastAPI + SQLite + 沪深份额抓取 + 托盘常驻）。
- **v2.0**：彻底重构为 Win11 零后台架构；新增 `600px` 手动关闭置顶大卡片；升级至 31 只宽基 ETF 与 8 大宽基曲线自由切换；新增 8 宽基买卖估算方块、60日位置修正、20日中位数成交基线、连买连卖记忆与自动双锚标尺。
- **v3.0（GPT Pro 全面审阅吸收升级）**：
  - 采纳并修复 `P0-1 ~ P0-7`、`P1-1 ~ P1-6`、`P2-1 ~ P2-5`：严格 `VERIFIED` 完备性门控、拒绝无日期深交所快照、严格 12 只 `huijin_core` 核心池、东财多报告期时点一致锚点 + 时效周转率衰减、取消 15% 硬地板、分离原始/派生写入原子发布、长离线自动延伸回补窗口、跨进程 SQLite 租约 + 分通道 Outbox 重试、Pydantic 强校验 + 彻底防死锁、比率分位平滑自适应标尺 + 每日持久化、完整日 5 分钟 K 线与 DB 尾盘基线、同方向共振 + 调仓解耦、独立 `user_notes` 表、跨零区间完整展示与独立着色、严禁缺基准偷换沪深300、CSV 公式中和、PushPlus 升 HTTPS、公告分类标签。
  - 主动驳回未采纳项：`echarts.min.js` 缺失系审阅包剔除导致的误报；跳过单机 API Token 过度防御；跳过发散的全路径库存仿真。
- **v3.1**：
  - 修复无份额盘面初判将低位暴涨误标为 `PRELIM_SELL` 的问题（改为中性 `PRELIM_WATCH`），并将自动弹窗严格收紧为 `VERIFIED` 核实结论专属。
  - 新增单边大卖「鸽笼库存底线」校正，将 2026 H1 估算中值从 `-9,904亿` 精准校准至 `-13,098亿`。
  - 自动巡检从开机 30 秒注册表升级为 Win11 原生任务计划 `GJDJK_DailyCheck`（`StartWhenAvailable` 错过开机补跑 + 智能联网探活）。
  - 新增 4 月初/9 月初定期报告自动追更刷新机制，以及连续报错/连续缺数的自动健康巡检与橙红色 `HEALTH_ALERT` 弹窗预警。
- **v3.1.1（当前版）**：
  - 将默认晚间自动定时巡检时间由 `21:30` 调整至 `22:30`（同步更新源码默认配置、前端设置页、源码版及便携版 `开启晚间自动巡检.bat`），避免放量交易日沪深交易所延迟公布 ETF 份额导致漏检。

---

## 8. 换电脑迁移 & 开发测试常用命令

### 8.1 换机无缝迁移步骤（Windows 11）
1. 拷贝整个项目目录（**特别记得带上 `data/gjdjk.db`**，里面存着全部历史数据与你的个人复盘批注）。
2. 在新电脑安装 Python 3.12+，于项目根目录执行（PowerShell）：
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\pip.exe install -r requirements.txt
   ```
3. 注册每日 22:30 定时巡检任务（或在打开软件后的「提醒与设置」页点击保存）：
   ```powershell
   .\.venv\Scripts\python.exe -c "from src.autostart import set_autostart; print(set_autostart(True, '22:30'))"
   ```
4. 双击 `启动国家队监控.vbs` 启动看板（可右键创建桌面快捷方式并指向 `data\gjdjk.ico`）。

### 8.2 开发与验收命令速查
```powershell
# 1. 运行全部 15 项回归测试
.\.venv\Scripts\python.exe -m pytest -v

# 2. 检查前端 JS 语法
node --check web/app.js

# 3. 命令行手动执行一次完整智能同步（或加 --force 强制重刷）
.\.venv\Scripts\python.exe app.py --sync-only

# 4. 模拟每晚 22:30 定时巡检（跑完即退出，0 MB 驻留）
.\.venv\Scripts\python.exe app.py --scheduled-check --delay 0

# 5. 查看当前 Win11 计划任务状态
Get-ScheduledTask -TaskName "GJDJK_DailyCheck" | Format-List TaskName, State, Actions, Triggers

# 6. 交易所接口改版导致份额缺数时，优先升级 akshare
.\.venv\Scripts\pip.exe install -U akshare
```

### 8.3 后续可选演进方向（非紧急）
- **第二底牌数据源**：当东财 `RPT_F10_FUND_HOLDERTOP10` 滞后时（如当前尚未同步 `2026-06-30`），可补充巨潮定期报告或基金季报“单一投资者持有比例 ≥20%”解析作为第二锚点源。
- **年度监控池审视**：每 1～2 年检查一次是否出现国家队新重仓的超百亿新宽基 ETF，按需增补至 `config/watchlist.yaml`。
