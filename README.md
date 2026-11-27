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
   - 借助 Win11 原生任务计划程序，每晚 **22:30** 等沪深交易所公布当日官方 ETF 份额后自动静默跑一次核实（几秒退出，不驻留托盘）；若当晚关机错过，**下次开机联网后自动补跑**。
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

## 快速开始（Windows 11）

### 方式一：直接下载免安装便携版（推荐，无需装 Python）
1. 前往 **[Releases 最新版本页面](https://github.com/TRAAACER/ashare-national-team-radar/releases/latest)** 下载 `ashare-national-team-radar-v*-win64.zip`；
2. **完整解压**到本地固定目录（例如 `D:\GJDJK`）；
3. 双击 **`GJDJK.exe`** 打开独立浅色桌面看板（首次运行自动回填近 1 年历史行情与官方份额，约 1～2 分钟；点右上角 `×` 关窗即彻底退出，零后台占用）；
4. 双击 **`开启晚间自动巡检.bat`**（或在界面「提醒与设置」页开启），每晚 **22:30** 交易所公布份额后自动静默核实，关机错过下次开机自动补跑。

### 方式二：源码运行（Python 3.12+）
```powershell
git clone https://github.com/TRAAACER/ashare-national-team-radar.git
cd ashare-national-team-radar
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
```
- **打开桌面看板**：双击 `启动国家队监控.vbs`
- **开启/关闭每晚 22:30 自动定时巡检**：双击 `开启晚间自动巡检.bat` / `关闭自动巡检.bat`
- **命令行用法**：
  ```powershell
  .\.venv\Scripts\python.exe app.py                   # 打开独立桌面看板
  .\.venv\Scripts\python.exe app.py --scheduled-check # 执行一次盘后定时巡检并退出（0 MB 驻留）
  .\.venv\Scripts\python.exe app.py --sync-only       # 命令行立即同步并打印结果
  .\.venv\Scripts\python.exe -m pytest -v             # 运行全部 15 项回归测试
  ```

---

## 关键词（Keywords）

`A股` · `国家队` · `中央汇金` · `宽基ETF` · `沪深300ETF` · `ETF净申赎` · `类平准基金` · `资金流监控` · `akshare` · `Win11桌面应用` · `A-Share` · `China ETF Flow` · `Central Huijin`

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
