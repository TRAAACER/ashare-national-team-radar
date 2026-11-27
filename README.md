# A股国家队宽基 ETF 动向监控雷达 · A-Share National Team ETF Radar

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform Windows 11](https://img.shields.io/badge/Platform-Windows%2011-0078D6?logo=windows11&logoColor=white)](#快速开始windows-11)
[![License MIT](https://img.shields.io/badge/License-MIT-22C55E.svg)](LICENSE)
[![Zero Background](https://img.shields.io/badge/Daemon-Zero%20Background%20(0%20MB)-0EA5E9)](#设计哲学零云服务器零外部数据库零后台常驻)

面向 A 股个人投资者与量化复盘爱好者的本地化 **宽基 ETF 资金流（申购/赎回）与“国家队”（中央汇金、国新投资、中国诚通等类平准基金）动向监测桌面应用**。

> **A-Share National Team ETF Radar (`ashare-national-team-radar`)** — A local, zero-background Windows 11 desktop monitor tracking China A-share broad-based ETF share creation/redemption flows (CSI 300 / SSE 50 / CSI 500 / CSI 1000 / ChiNext / STAR 50 / CSI A500) and estimating Central Huijin / National Team market-stabilization activity with Point-in-Time periodic report anchors, pigeonhole inventory bounds, and adaptive dual-anchor thresholds.

> [!WARNING]
> **免责声明（Disclaimer）**：本项目所有行情、份额、前十大持有人及公告数据均来自上交所、深交所及公开财经信息平台。模型输出的“疑似国家队买卖区间”“异常信号强度”及定性结论均为基于公开份额与定期报告的量化统计推演，**仅用于数据监测与个人复盘研究，不构成任何投资建议或买卖依据**。股市有风险，决策请独立判断。

---

## 为什么做这个项目？（核心亮点）

相比常见的“只看单日沪深300成交额”或“拿全部宽基净申赎直接当国家队”的粗糙脚本，本项目重点解决了 5 个实战痛点：

1. **零云服务器、零外部数据库、零后台常驻（0 MB 空闲内存）**
   - 借助 Win11 原生任务计划程序，每晚 **21:30** 等沪深交易所公布当日官方 ETF 份额后自动静默跑一次核实（几秒退出，不驻留托盘）；若当晚关机错过，**下次开机联网后自动补跑**。
   - 仅在 **官方份额已核实（`VERIFIED`）出现重大异动**、**新官宣公告** 或 **数据源故障** 时，弹出 `600px` 宽、置顶、必须手动关闭的浅色提醒卡片（并支持飞书 / 企业微信 / Server酱 / PushPlus / 自定义 Webhook 推送）。
   - 平时双击 `启动国家队监控.vbs` 打开独立浅色桌面看板，点右上角 `×` 优雅落盘并彻底退出进程。
2. **严格 12 只中央汇金核心池门控，过滤散户与混合资金噪音**
   - 覆盖沪深双所 8 大宽基方向共 **31 只宽基与国新央企 ETF** 及 **8 大基准指数**；
   - 严格区分 **12 只中央汇金高控盘宽基核心池**（沪深300四巨头、上证50/180、中证500、中证1000四巨头）与混合资金池，过滤中证 A500、创业板等普通机构/散户申赎噪音；
   - 内置拆合份/折算自动识别（杜绝千亿级假净买入）与严格 `VERIFIED` 完备性门控（12 只核心缺一不可 + 沪深覆盖 ≥ 90%）。
3. **时点一致（Point-in-Time）多期底牌 + 单边大卖鸽笼底线**
   - 自动抓取多期历史定期报告（年报/半年报）并绑定法定公开日（零未来函数），每年 **4 月初/9 月初自动盯更新报告并全量重算**；
   - 引入报告年龄与累计份额周转率衰减，并叠加零未来函数的单边大卖 **「鸽笼库存底线」**（$H_t \le S_t$）：当连续赎回砸穿国家队实名持仓新低时防止过度打折。
4. **双锚自适应百分比标尺，跨越牛熊免人工调参**
   - 按「当日总规模比例锚 + 滚动 120 日 `|净流|/AUM` 比率分位锚」平滑伸缩（15~60 日线性渐进权重消除冷启动断崖），不管未来宽基 ETF 盘子翻倍还是缩水，告警门槛自动适配。
5. **可沉淀的每日复盘档案 + 数据源自动健康巡检**
   - 支持 8 大宽基趋势曲线自由切换、8 大宽基买卖估算方块、1 年（250+ 交易日）复盘日志筛选、独立永久保存的个人复盘笔记与标签，以及一键导出 **Markdown / CSV**（内置 Excel 公式注入防护）；
   - 一旦检测到连续 ≥2 天份额缺失或同步报错，自动定位上交所/深交所缺口并弹出醒目橙红色故障预警卡片。

---

## 历史实盘校核（2025-11 ～ 2026-11，249 个交易日）

| 校核维度 | 客观参照基准 | 本项目 v3.1 实测结果 |
|---|---|---|
| **底层资金流精度**（2026-09-01 ~ 11-26，9 大宽基） | 权威媒体同口径累计 `6,759.6亿` / 单日峰值 `895.7亿` | **`+6,759.6 亿` / `+895.7 亿`（误差 0.0 亿）** |
| **抗误报压力测试**（2025-11 ~ 12，A500 净申购 `+961.5亿`） | 2025 年报实锤中央汇金核心池几乎未动 | **0 天强买、0 天强卖**（精准过滤混合资金噪音） |
| **单边大减仓校核**（2026 上半年 1 ~ 6 月） | 权威半年报测算汇金减持约 `1.3 ~ 1.4 万亿` | 估算区间 **`-15,690 ~ -10,506 亿`（中值 `-13,098 亿`）** |
| **逆周期行为规律**（全 249 日） | 平准基金“越跌越买、越涨越卖”特征 | 买入日大盘下跌占比 **61.9%**；强卖日后 +20 日胜率仅 **26%** |

> **实战提示**：国家队买入信号属于**左侧分批托底**，不代表次日立刻见底反弹；而连续大额逢高减仓信号对中短期降温压制的警示意义更显著。

---

## 快速开始（Windows 11）

### 1. 克隆并安装环境（Python 3.12+）
```powershell
git clone https://github.com/TRAAACER/ashare-national-team-radar.git
cd ashare-national-team-radar
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
```

### 2. 日常使用
- **打开桌面看板**：双击 `启动国家队监控.vbs`（首次运行自动回填近 1 年历史行情与官方份额，约 1～2 分钟；关窗即彻底退出）
- **开启/关闭每晚 21:30 自动定时巡检**：在软件内「提醒与设置」页一键开关并自定义时间，或直接双击 `开启晚间自动巡检.bat` / `关闭自动巡检.bat`
- **命令行用法**：
  ```powershell
  .\.venv\Scripts\python.exe app.py                   # 打开独立桌面看板
  .\.venv\Scripts\python.exe app.py --scheduled-check # 执行一次盘后定时巡检并退出（0 MB 驻留）
  .\.venv\Scripts\python.exe app.py --sync-only       # 命令行立即同步并打印结果
  .\.venv\Scripts\python.exe -m pytest -v             # 运行全部 15 项回归测试
  ```

---

## 技术栈与架构文档

- **后端 / 桌面**：Python 3.12 · FastAPI · SQLite (WAL) · `pywebview` (Edge WebView2) · `akshare`
- **前端**：原生 HTML/CSS/JS · 本地离线 ECharts（无外网 CDN 依赖）
- **详细开发交接文档**：完整的模块说明、量化公式推导、数据源排障手册、数据库表结构与换机迁移步骤，请查阅 **[PROJECT_HANDOFF.md](PROJECT_HANDOFF.md)**。

## 关键词（Keywords）

`A股` · `国家队` · `中央汇金` · `宽基ETF` · `沪深300ETF` · `ETF净申赎` · `类平准基金` · `资金流监控` · `akshare` · `Win11桌面应用` · `A-Share` · `China ETF Flow` · `Central Huijin`

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
