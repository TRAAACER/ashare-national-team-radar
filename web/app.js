/* 国家队动向监控 (GJDJK) 前端交互逻辑 v2 */

const state = {
  currentView: "overview",
  trendDays: 60,
  selectedBenchmark: "000300",
  flowScope: "GROUP",
  flowMetric: "NT", // 默认展示国家队估算买卖量，可选 POOL（全池净申赎）
  selectedDate: null,
  selectedGroupFilter: "ALL",
  dashboardData: null,
  journalVerdict: "ALL",
  journalItems: [],
  drawerDate: null,
  charts: {},
  wasRunning: false,
};

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

function $(id) {
  return document.getElementById(id);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
}

function showToast(msg, duration = 2600) {
  const el = $("app-toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => el.classList.add("hidden"), duration);
}

async function apiGet(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

async function apiSend(url, method, body) {
  const r = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((x) => x.msg || JSON.stringify(x)).join("；")
      : data.message || data.detail || `HTTP ${r.status}`;
    throw new Error(detail);
  }
  return data;
}

function fmtSignedYi(val, digits = 1) {
  if (val === null || val === undefined || Number.isNaN(val)) return "--";
  const sign = val > 0 ? "+" : "";
  return `${sign}${Number(val).toFixed(digits)}亿`;
}

function fmtPct(val) {
  if (val === null || val === undefined) return "--";
  const sign = val > 0 ? "+" : "";
  return `${sign}${Number(val).toFixed(2)}%`;
}

function ntIntervalDirection(low, high) {
  if (low === null || high === null || low === undefined || high === undefined) return "flat";
  if (low < -0.05 && high > 0.05) return "mixed";
  if (low >= -0.05 && high > 0.05) return "buy";
  if (high <= 0.05 && low < -0.05) return "sell";
  return "flat";
}

function ntDirColorClass(dir) {
  if (dir === "buy") return "text-red";
  if (dir === "sell") return "text-green";
  return "text-muted";
}

function fmtNtRange(low, high) {
  if (low === null || high === null || low === undefined || high === undefined) {
    return "份额待披露";
  }
  const dir = ntIntervalDirection(low, high);
  if (dir === "mixed") {
    return `方向分歧 ${low.toFixed(1)} ~ +${high.toFixed(1)}亿`;
  }
  if (dir === "sell") {
    return `净卖 ${Math.abs(Math.min(0, high)).toFixed(1)} ~ ${Math.abs(low).toFixed(1)}亿`;
  }
  if (dir === "buy") {
    return `净买 ${Math.max(0, low).toFixed(1)} ~ ${high.toFixed(1)}亿`;
  }
  return "基本未动 (0.0亿)";
}

function fmtShortNtBlock(low, high, netFlow) {
  if (netFlow === null || netFlow === undefined) return "待份额";
  if (low === null || high === null || low === undefined || high === undefined) {
    return Math.abs(netFlow) < 1.0 ? "基本未动" : fmtSignedYi(netFlow, 1);
  }
  const dir = ntIntervalDirection(low, high);
  if (dir === "mixed") {
    return `分歧 ${low.toFixed(1)}~+${high.toFixed(1)}亿`;
  }
  if (Math.abs(low) < 0.8 && Math.abs(high) < 0.8 && Math.abs(netFlow) < 2.0) {
    return "基本未动";
  }
  if (dir === "buy") {
    return `估买 ${Math.max(0, low).toFixed(1)}~${high.toFixed(1)}亿`;
  }
  if (dir === "sell") {
    return `估卖 ${Math.abs(Math.min(0, high)).toFixed(1)}~${Math.abs(low).toFixed(1)}亿`;
  }
  return "基本未动";
}

function flowColorClass(val) {
  if (val === null || val === undefined) return "text-muted";
  if (val > 0.05) return "text-red";
  if (val < -0.05) return "text-green";
  return "";
}

function verdictStyleClass(code) {
  if (["STRONG_BUY", "MODERATE_BUY"].includes(code)) return "buy";
  if (["STRONG_SELL", "MODERATE_SELL"].includes(code)) return "sell";
  if (code === "ROTATION") return "rotation";
  if (["PRELIM_RESCUE", "PRELIM_SELL", "PRELIM_WATCH"].includes(code)) return "prelim";
  return "quiet";
}

function stageBadge(stage, meta) {
  if (stage === "VERIFIED") return { text: "沪深份额已核实", cls: "verified" };
  if (stage === "SHARES_PARTIAL") {
    const pct = meta?.completeness_pct;
    return { text: pct ? `部分份额 (${pct}%) · 待补全` : "部分份额已出 · 待补全", cls: "partial" };
  }
  return { text: "收盘盘面初判 · 待份额", cls: "" };
}

function fmtVolumeDesc(ratio) {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return "--";
  if (ratio >= 2.2) return `巨量(${ratio.toFixed(1)}倍)`;
  if (ratio >= 1.25) return `放量 +${Math.round((ratio - 1) * 100)}%`;
  if (ratio >= 0.8) return "量能常态";
  return `缩量 -${Math.round((1 - ratio) * 100)}%`;
}

function getWeekday(dateStr) {
  const dt = new Date(dateStr + "T00:00:00");
  return Number.isNaN(dt.getTime()) ? "" : WEEKDAYS[dt.getDay()];
}

function syncScopeControls() {
  const isSseComposite = state.selectedBenchmark === "000001";
  const btnGroup = $("btn-scope-group");
  if (btnGroup) {
    btnGroup.disabled = isSseComposite;
    btnGroup.title = isSseComposite ? "上证指数无单一对应宽基组，仅支持全宽基合计" : "查看所选指数对应宽基组";
  }
  if (isSseComposite && state.flowScope === "GROUP") {
    state.flowScope = "ALL";
  }
  $("trend-scope-tabs").querySelectorAll(".seg-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.scope === state.flowScope);
  });
}

/* ==================== 导航与初始化 ==================== */
function switchView(viewName) {
  state.currentView = viewName;
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === viewName);
  });
  document.querySelectorAll(".view").forEach((sec) => {
    sec.classList.toggle("active", sec.id === `view-${viewName}`);
  });

  if (viewName === "overview") {
    loadDashboard();
    setTimeout(() => Object.values(state.charts).forEach((c) => c && c.resize()), 60);
  } else if (viewName === "journal") {
    loadJournal();
  } else if (viewName === "holdings") {
    loadHoldingsAndAnnouncements();
  } else if (viewName === "settings") {
    loadSettings();
  }
}

/* ==================== 后台同步状态轮询 ==================== */
async function pollStatus() {
  try {
    const st = await apiGet("/api/status");
    const dot = $("sidebar-status-dot");
    const txt = $("sidebar-status-text");
    const banner = $("sync-banner");
    const btnSync = $("btn-sync-now");

    $("nav-log-count").textContent = st.total_logs ?? 0;
    $("sidebar-latest-date").textContent = `最新交易日：${st.latest_trade_date || "尚无数据"}`;
    const ai = st.autostart_info || {};
    $("sidebar-autostart").textContent = ai.enabled
      ? `定时巡检：每日 ${ai.schedule_time || "22:30"}`
      : "定时巡检：未开启";

    // 渲染顶部数据源健康预警条（仅有严重 error/warning 故障时显现，平时完全隐藏）
    const health = st.health || {};
    const hBanner = $("health-banner");
    const errIssues = (health.issues || []).filter((i) => i.severity === "error" || i.severity === "warning");
    if (hBanner) {
      if (errIssues.length) {
        hBanner.classList.remove("hidden");
        $("health-banner-title").textContent = `⚠ 数据源异常预警：${errIssues[0].title}`;
        $("health-banner-detail").textContent = `${errIssues[0].detail} ${errIssues[0].action_hint || ""}`;
      } else {
        hBanner.classList.add("hidden");
      }
    }

    if (st.is_running) {
      dot.className = "status-dot running";
      txt.textContent = "正在同步...";
      banner.classList.remove("hidden");
      $("sync-banner-text").textContent = st.message || "正在更新数据...";
      $("sync-banner-pct").textContent = `${st.progress || 0}%`;
      $("sync-progress-fill").style.width = `${st.progress || 0}%`;
      btnSync.disabled = true;
      state.wasRunning = true;
    } else {
      banner.classList.add("hidden");
      btnSync.disabled = false;
      if (st.last_error || errIssues.length) {
        dot.className = "status-dot error";
        txt.textContent = errIssues.length ? "数据源异常预警" : "上次同步异常";
      } else {
        dot.className = "status-dot";
        txt.textContent = "本地已同步";
      }
      if (state.wasRunning) {
        state.wasRunning = false;
        showToast(st.message || "同步更新完成");
        if (state.currentView === "overview") loadDashboard();
        if (state.currentView === "journal") loadJournal();
        if (state.currentView === "holdings") loadHoldingsAndAnnouncements();
      }
    }
  } catch (e) {
    console.warn("status poll failed", e);
  }
}

/* ==================== 视图 1：今日全景 ==================== */
async function loadDashboard() {
  try {
    syncScopeControls();
    const q = new URLSearchParams({
      days: String(state.trendDays),
      benchmark: state.selectedBenchmark,
      flow_scope: state.flowScope,
    });
    if (state.selectedDate) q.set("trade_date", state.selectedDate);
    const data = await apiGet(`/api/dashboard?${q.toString()}`);
    state.dashboardData = data;
    if (data.flow_scope && data.flow_scope !== state.flowScope) {
      state.flowScope = data.flow_scope;
      syncScopeControls();
    }

    // 动态同步配置中的标的池数量（消灭硬编码漂移）
    if (data.etf_total_count && data.core_pool_count) {
      $("overview-subtitle").textContent = `沪深双所 ${data.etf_total_count} 只宽基 ETF 份额净申赎 + ${data.core_pool_count} 只汇金核心池五维共振识别`;
      $("etf-table-desc").textContent = `覆盖沪深双所 ${data.etf_total_count} 只主力宽基与国新央企 ETF（成交额含估算标注）`;
    }

    renderDateSelector(data.available_dates || [], data.latest?.trade_date);
    renderHero(data.latest);
    renderGroupBlocks(data.latest?.group_flows || []);
    renderWindowStats(data.window_stats);
    renderLamps(data.latest?.lamps || []);
    // 图表独立容错，避免图表组件异常阻断下方 ETF 明细表渲染
    try {
      renderTrendChart(data.trend || [], data.benchmark_selected, data.flow_scope, data.groups_meta || [], data.missing_flow_days || 0);
    } catch (ce) {
      console.error("Trend chart render error:", ce);
    }
    try {
      renderGroupsChart(data.latest?.group_flows || []);
    } catch (ce) {
      console.error("Groups chart render error:", ce);
    }
    renderGroupPills(data.groups_meta || []);
    renderEtfTable(data.etf_details || [], data.groups_meta || []);
  } catch (e) {
    console.error(e);
    showToast("加载看板失败：" + e.message);
  }
}

function renderGroupBlocks(groups) {
  const grid = $("group-blocks-grid");
  if (!groups.length) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:span 4;">暂无各宽基买卖明细</div>`;
    return;
  }
  grid.innerHTML = groups
    .map((g) => {
      const nf = g.net_flow_yi;
      const ntDir = ntIntervalDirection(g.nt_est_low_yi, g.nt_est_high_yi);
      const cardCls =
        nf === null || nf === undefined
          ? "flat"
          : ntDir === "buy" && (g.nt_est_high_yi ?? 0) >= 1.0
          ? "buy"
          : ntDir === "sell" && (g.nt_est_low_yi ?? 0) <= -1.0
          ? "sell"
          : "flat";
      const isCore = g.purity === "核心重仓" || g.purity === "汇金主力";
      const isSelected = g.benchmark_code === state.selectedBenchmark;
      const ntText = fmtShortNtBlock(g.nt_est_low_yi, g.nt_est_high_yi, nf);
      const ntCls = ntDirColorClass(ntDir);
      return `
        <div class="gblock ${cardCls} ${isSelected ? "active-bm" : ""}" data-bm="${escapeHtml(g.benchmark_code || "000300")}" title="点击切换趋势图到 ${escapeHtml(g.group_name)}">
          <div class="gblock-top">
            <div class="gblock-name-wrap">
              <span class="gblock-name">${escapeHtml(g.group_name)}</span>
              ${g.purity ? `<span class="gblock-purity ${isCore ? "core" : ""}">${escapeHtml(g.purity)}</span>` : ""}
            </div>
            <span class="gblock-idx-pct ${flowColorClass(g.index_pct)}">${fmtPct(g.index_pct)}</span>
          </div>
          <div class="gblock-nt-main ${ntCls}">${escapeHtml(ntText)}</div>
          <div class="gblock-meta">
            <span>全池净申赎 <strong class="${flowColorClass(nf)}">${fmtSignedYi(nf, 1)}</strong></span>
            <span>${escapeHtml(g.volume_desc || fmtVolumeDesc(g.avg_amount_ratio))}</span>
          </div>
        </div>
      `;
    })
    .join("");

  grid.querySelectorAll(".gblock").forEach((el) => {
    el.onclick = () => {
      const bm = el.dataset.bm;
      if (bm) {
        state.selectedBenchmark = bm;
        state.flowScope = bm === "000001" ? "ALL" : "GROUP";
        $("trend-benchmark-select").value = bm;
        syncScopeControls();
        loadDashboard();
      }
    };
  });
}

function renderDateSelector(dates, activeDate) {
  const sel = $("overview-date-select");
  if (!dates.length) {
    sel.innerHTML = `<option value="">等待初始化...</option>`;
    return;
  }
  sel.innerHTML = dates
    .map((d) => `<option value="${d}" ${d === activeDate ? "selected" : ""}>${d} (${getWeekday(d)})</option>`)
    .join("");
}

function renderHero(latest) {
  if (!latest) {
    $("hero-verdict-pill").textContent = "首次数据回填中";
    $("hero-verdict-pill").className = "verdict-pill prelim";
    $("hero-summary").textContent = "系统正在自动拉取近 1 年（约 250 个交易日）核心宽基 ETF 行情与交易所份额，请留意顶部进度条，完成后将自动呈现全景视图。";
    return;
  }
  const vCls = verdictStyleClass(latest.verdict_code);
  const pill = $("hero-verdict-pill");
  pill.textContent = latest.verdict_label || "平静观望";
  pill.className = `verdict-pill ${vCls}`;

  const sb = stageBadge(latest.data_stage, latest.meta);
  const stEl = $("hero-stage-pill");
  stEl.textContent = sb.text;
  stEl.className = `stage-pill ${sb.cls}`;

  $("hero-date-tag").textContent = `${latest.trade_date} ${getWeekday(latest.trade_date)}`;
  $("hero-summary").textContent = latest.auto_summary || "";

  const rotBox = $("hero-rotation-callout");
  if (latest.rotation_desc) {
    rotBox.classList.remove("hidden");
    $("hero-rotation-text").textContent = latest.rotation_desc;
  } else {
    rotBox.classList.add("hidden");
  }

  const setIdx = (id, pct) => {
    const el = $(id);
    el.textContent = fmtPct(pct);
    el.className = flowColorClass(pct);
  };
  setIdx("idx-sh", latest.sh_index_pct);
  setIdx("idx-hs300", latest.hs300_pct);
  setIdx("idx-zz1000", latest.zz1000_pct);
  setIdx("idx-cyb", latest.cyb_pct);

  const poolEl = $("metric-pool-flow");
  poolEl.textContent = latest.total_net_flow_yi !== null ? fmtSignedYi(latest.total_net_flow_yi, 2) : "待份额披露";
  poolEl.className = `metric-value ${flowColorClass(latest.total_net_flow_yi)}`;
  $("metric-pool-split").textContent =
    latest.gross_inflow_yi !== null
      ? `宽基流入 +${latest.gross_inflow_yi.toFixed(1)}亿 ｜ 流出 -${latest.gross_outflow_yi.toFixed(1)}亿`
      : "交易所每日盘后更新份额后自动计算";

  const ntEl = $("metric-nt-range");
  const ntDir = ntIntervalDirection(latest.nt_est_low_yi, latest.nt_est_high_yi);
  ntEl.textContent = fmtNtRange(latest.nt_est_low_yi, latest.nt_est_high_yi);
  ntEl.className = `metric-value ${ntDirColorClass(ntDir)}`;
  const anchorRd = latest.meta?.anchor_report_date;
  const anchorAge = latest.meta?.anchor_age_days;
  const anchorNote = anchorRd ? `｜底牌锚点 ${anchorRd}${anchorAge !== null && anchorAge !== undefined ? `（已过${anchorAge}天）` : ""}` : "｜按结构先验估算";
  $("metric-nt-mid").textContent =
    latest.nt_est_mid_yi !== null
      ? `估算中值 ${fmtSignedYi(latest.nt_est_mid_yi, 1)}${anchorNote}`
      : "当前处于收盘量能初判阶段";

  const score = Math.round(latest.nt_confidence || 0);
  $("hero-score-num").textContent = score;
  const ring = $("hero-score-ring");
  const circumference = 2 * Math.PI * 46;
  ring.style.strokeDasharray = String(circumference);
  ring.style.strokeDashoffset = String(circumference * (1 - score / 100));
  ring.style.stroke = vCls === "buy" ? "#DC2626" : vCls === "sell" ? "#16A34A" : vCls === "rotation" ? "#2563EB" : "#64748B";
}

function renderWindowStats(ws) {
  if (!ws) return;
  const map = [
    ["d5", "ws-5"],
    ["d20", "ws-20"],
    ["d60", "ws-60"],
    ["d250", "ws-250"],
  ];
  for (const [k, prefix] of map) {
    const item = ws[k];
    if (!item) continue;
    const valEl = $(`${prefix}-net`);
    valEl.textContent = fmtSignedYi(item.net_flow_yi, 1);
    valEl.className = `stat-val ${flowColorClass(item.net_flow_yi)}`;
    $(`${prefix}-sub`).textContent = `估国家队 ${fmtSignedYi(item.nt_mid_yi, 1)} · 买${item.buy_days}/卖${item.sell_days}/调${item.rotation_days}天`;
  }
}

function renderLamps(lamps) {
  const grid = $("lamps-grid");
  if (!lamps.length) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:span 5;">暂无信号灯数据</div>`;
    return;
  }
  grid.innerHTML = lamps
    .map(
      (l) => `
      <div class="lamp-card ${escapeHtml(l.state)}">
        <div class="lamp-top">
          <div class="lamp-name"><span class="lamp-dot ${escapeHtml(l.state)}"></span>${escapeHtml(l.name)}</div>
          <span class="lamp-score">${l.state === "na" ? "--" : Math.round(l.score) + "分"}</span>
        </div>
        <div class="lamp-val">${escapeHtml(l.value_text)}</div>
        <div class="lamp-thresh">阈值：${escapeHtml(l.threshold_text)}</div>
        <div class="lamp-detail">${escapeHtml(l.detail)}</div>
      </div>
    `
    )
    .join("");
}

function renderTrendChart(trend, bmSelected, flowScope, groupsMeta, missingFlowDays) {
  const dom = $("chart-trend");
  if (typeof echarts === "undefined") {
    dom.innerHTML = `<div class="empty-state">图表组件未加载（请检查 /static/vendor/echarts.min.js）</div>`;
    return;
  }
  if (!state.charts.trend) {
    state.charts.trend = echarts.init(dom);
    state.charts.trend.on("click", (params) => {
      if (params && params.name) {
        state.selectedDate = params.name;
        loadDashboard();
      }
    });
  }
  const isNt = state.flowMetric === "NT";
  const bmName = bmSelected?.name || "沪深300";
  const isGroup = flowScope === "GROUP" && bmSelected?.group && bmSelected.group !== "SOE";
  const grpMatch = (groupsMeta || []).find((g) => g.id === bmSelected?.group);
  const scopePrefix = isGroup ? (grpMatch?.name || bmName) : "全宽基";
  const metricLabel = isNt ? "国家队估算买卖" : "全池净申赎";
  const barTitle = `${scopePrefix}${metricLabel}(亿)`;
  const cumTitle = isNt ? `${scopePrefix}累计国家队估算(亿)` : `${scopePrefix}累计净申赎(亿)`;
  $("trend-chart-title").textContent = `${scopePrefix}${metricLabel} vs ${bmName}`;

  const showCum = $("toggle-cum-line").checked;
  const dates = trend.map((x) => x.trade_date);
  const flows = trend.map((x) => {
    const raw = isNt ? x.nt_est_mid_yi : x.total_net_flow_yi;
    if (raw === null || raw === undefined) return null;
    return {
      value: raw,
      itemStyle: {
        color: raw >= 0 ? "#EF4444" : "#22C55E",
        borderRadius: raw >= 0 ? [3, 3, 0, 0] : [0, 0, 3, 3],
      },
    };
  });
  // 禁止跨指数代填：缺失基准点位时保留 null 断线
  const bmLine = trend.map((x) => (x.benchmark_close !== null && x.benchmark_close !== undefined ? x.benchmark_close : null));
  const cumLine = trend.map((x) => (isNt ? x.cum_nt_mid_yi : x.cum_flow_yi));

  const series = [
    {
      name: barTitle,
      type: "bar",
      data: flows,
      barMaxWidth: 16,
      yAxisIndex: 0,
      itemStyle: { color: "#EF4444" },
    },
    {
      name: `${bmName}走势`,
      type: "line",
      data: bmLine,
      yAxisIndex: 1,
      showSymbol: false,
      connectNulls: false,
      smooth: true,
      lineStyle: { color: "#1E293B", width: 2 },
      itemStyle: { color: "#1E293B" },
    },
  ];
  if (showCum) {
    series.push({
      name: cumTitle,
      type: "line",
      data: cumLine,
      yAxisIndex: 0,
      showSymbol: false,
      smooth: true,
      lineStyle: { color: "#2563EB", width: 2, type: "dashed" },
      itemStyle: { color: "#2563EB" },
    });
  }

  state.charts.trend.setOption(
    {
      tooltip: {
        trigger: "axis",
        backgroundColor: "rgba(255,255,255,0.97)",
        borderColor: "#E2E8F0",
        textStyle: { color: "#0F172A", fontSize: 12 },
        formatter: (params) => {
          const idx = params[0]?.dataIndex;
          const row = trend[idx];
          if (!row) return "";
          const nf = row.total_net_flow_yi !== null && row.total_net_flow_yi !== undefined ? fmtSignedYi(row.total_net_flow_yi, 2) : "待披露";
          const nt = fmtNtRange(row.nt_est_low_yi, row.nt_est_high_yi);
          const ntMid = row.nt_est_mid_yi !== null && row.nt_est_mid_yi !== undefined ? fmtSignedYi(row.nt_est_mid_yi, 1) : "--";
          const bClose = row.benchmark_close !== null && row.benchmark_close !== undefined ? row.benchmark_close : "当日缺失";
          const bPct = row.benchmark_pct !== null && row.benchmark_pct !== undefined ? ` (${fmtPct(row.benchmark_pct)})` : "";
          const missHint = missingFlowDays > 0 ? ` <span style="color:#94A3B8;">(区间缺${missingFlowDays}日)</span>` : "";
          return `
            <div style="font-weight:700;margin-bottom:4px;">${escapeHtml(row.trade_date)} · ${escapeHtml(row.verdict_label || "")}</div>
            <div>${escapeHtml(scopePrefix)}国家队估算：<b>${escapeHtml(nt)}</b>（中值 ${escapeHtml(ntMid)}）</div>
            <div>${escapeHtml(scopePrefix)}全池净申赎：<b>${escapeHtml(nf)}</b></div>
            <div>${escapeHtml(bmName)}：<b>${escapeHtml(String(bClose))}</b>${escapeHtml(bPct)}</div>
            <div>${escapeHtml(cumTitle)}：<b>${escapeHtml(fmtSignedYi(isNt ? row.cum_nt_mid_yi : row.cum_flow_yi, 1))}</b>${missHint}</div>
          `;
        },
      },
      legend: {
        top: 2,
        left: "center",
        itemGap: 18,
        textStyle: { fontSize: 12, color: "#475569" },
      },
      grid: { left: 56, right: 62, top: 48, bottom: 28 },
      xAxis: {
        type: "category",
        data: dates,
        axisLine: { lineStyle: { color: "#CBD5E1" } },
        axisLabel: { color: "#64748B", fontSize: 11 },
      },
      yAxis: [
        {
          type: "value",
          name: isNt ? "国家队估算(亿)" : "净申赎(亿)",
          nameGap: 14,
          nameTextStyle: { color: "#8A94A6", fontSize: 11, align: "left" },
          splitLine: { lineStyle: { color: "#F1F5F9" } },
          axisLabel: { color: "#64748B", fontSize: 11 },
        },
        {
          type: "value",
          scale: true,
          name: "",
          splitLine: { show: false },
          axisLabel: { color: "#64748B", fontSize: 11 },
        },
      ],
      series,
    },
    true
  );
}

function renderGroupsChart(groups) {
  const dom = $("chart-groups");
  if (typeof echarts === "undefined") {
    dom.innerHTML = `<div class="empty-state">图表组件未加载</div>`;
    return;
  }
  if (!state.charts.groups) {
    state.charts.groups = echarts.init(dom);
  }
  const ordered = [...groups].reverse();
  const names = ordered.map((g) => g.group_name);
  const vals = ordered.map((g) => {
    const v = g.net_flow_yi;
    if (v === null || v === undefined) return null;
    return {
      value: v,
      itemStyle: {
        color: v >= 0 ? "#EF4444" : "#22C55E",
        borderRadius: v >= 0 ? [0, 4, 4, 0] : [4, 0, 0, 4],
      },
    };
  });
  state.charts.groups.setOption(
    {
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: (params) => {
          const g = ordered[params[0]?.dataIndex];
          if (!g) return "";
          return `<b>${escapeHtml(g.group_name)}</b><br/>全池净申赎：${escapeHtml(fmtSignedYi(g.net_flow_yi, 2))}<br/>国家队估算：${escapeHtml(fmtNtRange(g.nt_est_low_yi, g.nt_est_high_yi))}<br/>总成交：${g.amount_yi}亿<br/>量能比：${g.avg_amount_ratio}x`;
        },
      },
      grid: { left: 68, right: 32, top: 14, bottom: 24 },
      xAxis: {
        type: "value",
        splitLine: { lineStyle: { color: "#F1F5F9" } },
        axisLabel: { color: "#64748B", fontSize: 11 },
      },
      yAxis: {
        type: "category",
        data: names,
        axisLine: { lineStyle: { color: "#CBD5E1" } },
        axisLabel: { color: "#334155", fontSize: 12, fontWeight: 600 },
      },
      series: [
        {
          type: "bar",
          data: vals,
          barMaxWidth: 20,
          label: {
            show: true,
            position: "right",
            fontSize: 11,
            color: "#334155",
            formatter: (p) => (p.value === null || p.value === undefined ? "" : p.value === 0 ? "0" : `${p.value > 0 ? "+" : ""}${p.value}`),
          },
        },
      ],
    },
    true
  );
}

function renderGroupPills(groupsMeta) {
  const wrap = $("etf-group-pills");
  const list = [{ id: "ALL", name: "全部" }, ...groupsMeta];
  wrap.innerHTML = list
    .map(
      (g) => `<button class="pill ${state.selectedGroupFilter === g.id ? "active" : ""}" data-gid="${escapeHtml(g.id)}">${escapeHtml(g.name)}</button>`
    )
    .join("");
  wrap.querySelectorAll(".pill").forEach((btn) => {
    btn.onclick = () => {
      state.selectedGroupFilter = btn.dataset.gid;
      renderGroupPills(groupsMeta);
      renderEtfTable(state.dashboardData?.etf_details || [], groupsMeta);
    };
  });
}

function renderEtfTable(rows, groupsMeta) {
  const tbody = $("etf-table-body");
  const gMap = Object.fromEntries(groupsMeta.map((g) => [g.id, g.name]));
  const filtered =
    state.selectedGroupFilter === "ALL"
      ? rows
      : rows.filter((r) => r.group_id === state.selectedGroupFilter);

  if (!filtered.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="empty-state">暂无当日 ETF 明细数据</td></tr>`;
    return;
  }

  tbody.innerHTML = filtered
    .map((r) => {
      const amtYi = r.amount ? (r.amount / 1e8).toFixed(2) + (r.amount_is_estimated ? "*" : "") : "--";
      const amtTip = r.amount_is_estimated ? "成交额按 成交量×(O+H+L+C)/4 近似估算" : "接口直出成交额";
      const sharesYi = r.total_shares ? (r.total_shares / 1e8).toFixed(2) : "--";
      const deltaYi =
        r.flow_status === "split"
          ? "折算过滤"
          : r.share_delta !== null && r.share_delta !== undefined
          ? `${r.share_delta > 0 ? "+" : ""}${(r.share_delta / 1e8).toFixed(2)}`
          : "--";
      const flowTxt = r.flow_status === "split" ? "折算过滤" : fmtSignedYi(r.net_flow_yi, 2);
      const tailTxt = r.tail_share !== null && r.tail_share !== undefined ? `${(r.tail_share * 100).toFixed(1)}%` : "--";
      const ratioTxt = fmtVolumeDesc(r.amount_ratio);
      const ratioTip = r.amount_ratio ? `当日成交额约为近20日中位数的 ${Number(r.amount_ratio).toFixed(2)} 倍` : "";
      const ratioCls = r.amount_ratio >= 1.8 ? "text-red" : r.amount_ratio >= 1.35 ? "text-blue" : "";
      return `
        <tr>
          <td class="code-cell">${escapeHtml(r.code)}<span class="code-sub">${escapeHtml(r.name || "")}</span></td>
          <td><span class="group-chip">${escapeHtml(gMap[r.group_id] || r.group_id || "")}</span></td>
          <td class="num">${r.close ? Number(r.close).toFixed(3) : "--"}</td>
          <td class="num ${flowColorClass(r.pct_chg)}">${fmtPct(r.pct_chg)}</td>
          <td class="num" title="${amtTip}">${amtYi}</td>
          <td class="num ${ratioCls}" title="${ratioTip}">${escapeHtml(ratioTxt)}</td>
          <td class="num">${tailTxt}</td>
          <td class="num">${sharesYi}</td>
          <td class="num ${flowColorClass(r.share_delta)}">${deltaYi}</td>
          <td class="num ${flowColorClass(r.net_flow_yi)}">${flowTxt}</td>
        </tr>
      `;
    })
    .join("");
}

/* ==================== 视图 2：每日历史日志 ==================== */
function buildJournalQueryParams() {
  const q = new URLSearchParams({ verdict: state.journalVerdict });
  const sd = $("journal-start-date").value;
  const ed = $("journal-end-date").value;
  const kw = $("journal-keyword").value.trim();
  const onlyNote = $("journal-only-notes").checked;
  if (sd) q.set("start_date", sd);
  if (ed) q.set("end_date", ed);
  if (kw) q.set("keyword", kw);
  if (onlyNote) q.set("only_with_note", "true");
  return q;
}

async function loadJournal() {
  try {
    const q = buildJournalQueryParams();
    const res = await apiGet(`/api/logs?${q.toString()}`);
    state.journalItems = res.items || [];
    renderJournalList(state.journalItems);
  } catch (e) {
    showToast("加载历史日志失败：" + e.message);
  }
}

function renderJournalList(items) {
  const sumBar = $("journal-summary-bar");
  const totalNet = items.reduce((acc, x) => acc + (x.total_net_flow_yi || 0), 0);
  const buyCnt = items.filter((x) => ["STRONG_BUY", "MODERATE_BUY"].includes(x.verdict_code)).length;
  const sellCnt = items.filter((x) => ["STRONG_SELL", "MODERATE_SELL"].includes(x.verdict_code)).length;
  const rotCnt = items.filter((x) => x.verdict_code === "ROTATION" || (x.rotation_desc || "").trim()).length;
  const noteCnt = items.filter((x) => (x.user_note || "").trim() || (x.user_tags || "").trim()).length;

  sumBar.innerHTML = `
    <span>共 <b>${items.length}</b> 个交易日</span>
    <span>合计净申赎：<b class="${flowColorClass(totalNet)}">${fmtSignedYi(totalNet, 1)}</b></span>
    <span>疑似买入 <b class="text-red">${buyCnt}</b> 天 · 疑似卖出 <b class="text-green">${sellCnt}</b> 天 · 含调仓 <b class="text-blue">${rotCnt}</b> 天</span>
    <span>含个人批注 <b>${noteCnt}</b> 条</span>
  `;

  const container = $("journal-list");
  if (!items.length) {
    container.innerHTML = `<div class="card empty-state">当前筛选条件下暂无历史日志记录</div>`;
    return;
  }

  container.innerHTML = items
    .map((it) => {
      const vCls = verdictStyleClass(it.verdict_code);
      const sb = stageBadge(it.data_stage, it.meta);
      const dots = (it.lamps || [])
        .map((l) => `<span class="lamp-dot ${escapeHtml(l.state)}" title="${escapeHtml(l.name)}：${escapeHtml(l.value_text)}"></span>`)
        .join("");
      const tagsHtml = (it.user_tags_list || []).map((t) => `<span class="tag-badge">${escapeHtml(t)}</span>`).join("");
      const hasNote = (it.user_note || "").trim() || tagsHtml;
      const ntDir = ntIntervalDirection(it.nt_est_low_yi, it.nt_est_high_yi);
      const groupChips = (it.group_flows || [])
        .filter((g) => g.net_flow_yi !== null && Math.abs(g.net_flow_yi) >= 3.0)
        .sort((a, b) => Math.abs(b.net_flow_yi) - Math.abs(a.net_flow_yi))
        .slice(0, 5)
        .map((g) => {
          const gDir = ntIntervalDirection(g.nt_est_low_yi, g.nt_est_high_yi);
          const c = gDir === "buy" ? "buy" : gDir === "sell" ? "sell" : "";
          const txt = fmtShortNtBlock(g.nt_est_low_yi, g.nt_est_high_yi, g.net_flow_yi);
          return `<span class="j-block-chip ${c}">${escapeHtml(g.group_name)} · ${escapeHtml(txt)}</span>`;
        })
        .join("");
      return `
        <div class="journal-card" data-date="${escapeHtml(it.trade_date)}">
          <div class="j-top">
            <div class="j-left">
              <span class="j-date">${escapeHtml(it.trade_date)}</span>
              <span class="j-weekday">${getWeekday(it.trade_date)}</span>
              <span class="verdict-pill ${vCls}" style="padding:3px 10px;font-size:12px;">${escapeHtml(it.verdict_label)}</span>
              <span class="stage-pill ${sb.cls}">${escapeHtml(sb.text)}</span>
              <span class="idx-chip">沪深300 <strong class="${flowColorClass(it.hs300_pct)}">${fmtPct(it.hs300_pct)}</strong></span>
            </div>
            <div class="j-right">
              <div class="mini-lamps" title="五维信号灯缩略">${dots}</div>
              <div class="j-metric">
                <div class="j-metric-lbl">全池净申赎</div>
                <div class="j-metric-val ${flowColorClass(it.total_net_flow_yi)}">${fmtSignedYi(it.total_net_flow_yi, 1)}</div>
              </div>
              <div class="j-metric">
                <div class="j-metric-lbl">疑似国家队估算</div>
                <div class="j-metric-val ${ntDirColorClass(ntDir)}">${escapeHtml(fmtNtRange(it.nt_est_low_yi, it.nt_est_high_yi))}</div>
              </div>
              <div class="j-metric">
                <div class="j-metric-lbl">信号强度</div>
                <div class="j-metric-val">${Math.round(it.nt_confidence || 0)}</div>
              </div>
            </div>
          </div>
          <div class="j-summary">${escapeHtml(it.auto_summary || "")}</div>
          ${groupChips ? `<div class="j-blocks-strip">${groupChips}</div>` : ""}
          ${
            hasNote
              ? `<div class="j-note-preview"><span>📝 我的复盘：</span>${tagsHtml}<span>${escapeHtml(it.user_note || "")}</span></div>`
              : ""
          }
        </div>
      `;
    })
    .join("");

  container.querySelectorAll(".journal-card").forEach((card) => {
    card.onclick = () => openLogDrawer(card.dataset.date);
  });
}

/* ==================== 日志详情抽屉 & 个人笔记保存 ==================== */
async function openLogDrawer(tradeDate) {
  try {
    state.drawerDate = tradeDate;
    const { log, etf_details } = await apiGet(`/api/logs/${tradeDate}`);
    $("drawer-date-title").textContent = `${log.trade_date} (${getWeekday(log.trade_date)})`;
    $("drawer-verdict-sub").textContent = `${log.verdict_label} · 信号强度 ${Math.round(log.nt_confidence || 0)} · ${stageBadge(log.data_stage, log.meta).text}`;
    $("drawer-auto-summary").textContent = log.auto_summary || "";
    $("drawer-etf-title").textContent = `当日 ${(etf_details || []).length} 只宽基 ETF 明细`;

    $("drawer-group-blocks").innerHTML = (log.group_flows || [])
      .map((g) => {
        const nf = g.net_flow_yi;
        const gDir = ntIntervalDirection(g.nt_est_low_yi, g.nt_est_high_yi);
        const cls =
          nf === null || nf === undefined
            ? "flat"
            : gDir === "buy" && (g.nt_est_high_yi ?? 0) >= 1.0
            ? "buy"
            : gDir === "sell" && (g.nt_est_low_yi ?? 0) <= -1.0
            ? "sell"
            : "flat";
        const ntTxt = fmtShortNtBlock(g.nt_est_low_yi, g.nt_est_high_yi, nf);
        const ntCls = ntDirColorClass(gDir);
        return `
          <div class="gblock ${cls}" style="padding:9px 11px;cursor:default;">
            <div class="gblock-top" style="margin-bottom:3px;">
              <span class="gblock-name" style="font-size:12.5px;">${escapeHtml(g.group_name)}</span>
              <span class="gblock-idx-pct ${flowColorClass(g.index_pct)}">${fmtPct(g.index_pct)}</span>
            </div>
            <div class="${ntCls}" style="font-size:14px;font-weight:800;">${escapeHtml(ntTxt)}</div>
            <div style="font-size:11px;color:#8A94A6;">全池 <span class="${flowColorClass(nf)}">${fmtSignedYi(nf, 1)}</span></div>
          </div>
        `;
      })
      .join("");

    const ntDir = ntIntervalDirection(log.nt_est_low_yi, log.nt_est_high_yi);
    $("drawer-metrics").innerHTML = `
      <div class="metric-box" style="padding:10px 12px;">
        <div class="metric-label">全池净申赎</div>
        <div class="metric-value ${flowColorClass(log.total_net_flow_yi)}" style="font-size:17px;">${fmtSignedYi(log.total_net_flow_yi, 2)}</div>
      </div>
      <div class="metric-box highlight-box" style="padding:10px 12px;">
        <div class="metric-label">疑似国家队估算</div>
        <div class="metric-value ${ntDirColorClass(ntDir)}" style="font-size:15px;">${escapeHtml(fmtNtRange(log.nt_est_low_yi, log.nt_est_high_yi))}</div>
      </div>
      <div class="metric-box" style="padding:10px 12px;">
        <div class="metric-label">沪深300涨跌</div>
        <div class="metric-value ${flowColorClass(log.hs300_pct)}" style="font-size:17px;">${fmtPct(log.hs300_pct)}</div>
      </div>
    `;

    $("drawer-tags-input").value = log.user_tags || "";
    $("drawer-note-input").value = log.user_note || "";

    $("drawer-lamps").innerHTML = (log.lamps || [])
      .map(
        (l) => `
        <div class="drawer-lamp-row">
          <div>
            <div style="font-weight:700;display:flex;align-items:center;gap:6px;">
              <span class="lamp-dot ${escapeHtml(l.state)}"></span>${escapeHtml(l.name)}
              <span style="font-weight:500;color:#64748B;">${escapeHtml(l.value_text)}</span>
            </div>
            <div style="color:#475569;margin-top:3px;">${escapeHtml(l.detail)}</div>
          </div>
          <strong style="color:#475569;white-space:nowrap;">${l.state === "na" ? "--" : Math.round(l.score) + "分"}</strong>
        </div>
      `
      )
      .join("");

    $("drawer-etf-body").innerHTML = (etf_details || [])
      .map(
        (r) => `
        <tr>
          <td class="code-cell">${escapeHtml(r.code)}<span class="code-sub">${escapeHtml(r.name || "")}</span></td>
          <td class="num ${flowColorClass(r.pct_chg)}">${fmtPct(r.pct_chg)}</td>
          <td class="num">${r.amount ? (r.amount / 1e8).toFixed(2) : "--"}</td>
          <td class="num">${r.amount_ratio ? Number(r.amount_ratio).toFixed(2) + "x" : "--"}</td>
          <td class="num ${flowColorClass(r.net_flow_yi)}">${r.flow_status === "split" ? "折算过滤" : fmtSignedYi(r.net_flow_yi, 2)}</td>
        </tr>
      `
      )
      .join("");

    $("drawer-backdrop").classList.remove("hidden");
    $("log-drawer").classList.remove("hidden");
  } catch (e) {
    showToast("加载单日详情失败：" + e.message);
  }
}

function closeLogDrawer() {
  $("drawer-backdrop").classList.add("hidden");
  $("log-drawer").classList.add("hidden");
}

async function saveDrawerNote() {
  if (!state.drawerDate) return;
  try {
    await apiSend(`/api/logs/${state.drawerDate}/note`, "PUT", {
      user_note: $("drawer-note-input").value,
      user_tags: $("drawer-tags-input").value,
    });
    showToast(`已保存 ${state.drawerDate} 个人复盘笔记`);
    loadJournal();
  } catch (e) {
    showToast("保存失败：" + e.message);
  }
}

/* ==================== 视图 3：公告与底牌 ==================== */
async function loadHoldingsAndAnnouncements() {
  try {
    const [hData, aData] = await Promise.all([apiGet("/api/holdings"), apiGet("/api/announcements")]);
    $("h-report-date").textContent = hData.latest_report_date || "待同步";
    const ageTxt = hData.anchor_age_days !== null && hData.anchor_age_days !== undefined ? `锚点已距今 ${hData.anchor_age_days} 天` : "";
    const fetchTxt = hData.last_fetched_at ? `最近刷新 ${hData.last_fetched_at.slice(0, 16)}` : "";
    const perTxt = hData.period_count ? `已存 ${hData.period_count} 期历史` : "";
    $("h-report-meta").textContent = [ageTxt, perTxt, fetchTxt].filter(Boolean).join(" · ") || "来源：东财基金前十大持有人数据";

    $("h-total-val").textContent = hData.total_nt_value_yi ? `${hData.total_nt_value_yi.toLocaleString()} 亿` : "--";
    $("h-covered-cnt").textContent = `${hData.covered_etfs || 0} 只`;

    try {
      renderEntitiesChart(hData.by_entity || []);
    } catch (ce) {
      console.error("Entities chart render error:", ce);
    }
    renderHoldingsTable(hData.by_etf || []);
    renderAnnouncements(aData.items || []);
  } catch (e) {
    showToast("加载底牌与公告失败：" + e.message);
  }
}

function renderEntitiesChart(entities) {
  const dom = $("chart-entities");
  if (typeof echarts === "undefined") {
    dom.innerHTML = `<div class="empty-state">图表组件未加载</div>`;
    return;
  }
  if (!state.charts.entities) {
    state.charts.entities = echarts.init(dom);
  }
  const ordered = [...entities].reverse();
  state.charts.entities.setOption(
    {
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: (p) => {
          const item = ordered[p[0]?.dataIndex];
          return item ? `<b>${escapeHtml(item.entity_label)}</b> (${escapeHtml(item.entity_category)})<br/>覆盖 ETF：${item.etf_count} 只<br/>估算持仓：${item.est_value_yi} 亿元` : "";
        },
      },
      grid: { left: 120, right: 48, top: 12, bottom: 24 },
      xAxis: { type: "value", splitLine: { lineStyle: { color: "#F1F5F9" } }, axisLabel: { color: "#64748B", fontSize: 11 } },
      yAxis: {
        type: "category",
        data: ordered.map((x) => x.entity_label),
        axisLine: { lineStyle: { color: "#CBD5E1" } },
        axisLabel: { color: "#334155", fontSize: 12 },
      },
      series: [
        {
          type: "bar",
          data: ordered.map((x) => x.est_value_yi),
          barMaxWidth: 22,
          itemStyle: { color: "#2563EB", borderRadius: [0, 5, 5, 0] },
          label: { show: true, position: "right", fontSize: 11, color: "#1E293B", formatter: "{c}亿" },
        },
      ],
    },
    true
  );
}

function renderHoldingsTable(byEtf) {
  const tbody = $("holdings-table-body");
  if (!byEtf.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty-state">尚未同步定期报告底牌，请点击右上角刷新</td></tr>`;
    return;
  }
  tbody.innerHTML = byEtf
    .map((ef) => {
      const tags = (ef.holders || [])
        .map(
          (h) =>
            `<span class="holder-tag">${escapeHtml(h.entity_label)} ${h.hold_ratio ? h.hold_ratio.toFixed(1) + "%" : ""}${h.est_value_yi ? " (" + h.est_value_yi + "亿)" : ""}</span>`
        )
        .join("");
      return `
        <tr>
          <td class="code-cell">${escapeHtml(ef.code)}<span class="code-sub">${escapeHtml(ef.etf_name)}</span></td>
          <td>${escapeHtml(ef.report_date)}</td>
          <td class="num text-blue">${ef.nt_ratio_sum.toFixed(2)}%</td>
          <td class="num text-red">${ef.nt_value_yi.toFixed(2)}</td>
          <td style="white-space:normal;"><div class="holder-tags">${tags}</div></td>
        </tr>
      `;
    })
    .join("");
}

function renderAnnouncements(items) {
  const list = $("ann-list");
  if (!items.length) {
    list.innerHTML = `<div class="empty-state">近一年未检索到符合严格条件的国家队公告</div>`;
    return;
  }
  list.innerHTML = items
    .map((a) => {
      const cat = a.event_category || "主体股权/要约事件";
      const isEtfCat = cat.includes("ETF") || cat.includes("增持");
      const catStyle = isEtfCat
        ? "background:#FEF2F2;color:#DC2626;"
        : "background:#F1F5F9;color:#475569;";
      return `
      <div class="ann-item">
        <div>
          <a href="${escapeHtml(a.url || "#")}" target="_blank" rel="noopener">${escapeHtml(a.title)}</a>
          <div class="ann-meta">
            <span class="group-chip" style="${catStyle}">${escapeHtml(cat)}</span>
            <span class="group-chip" style="background:#EFF6FF;color:#1D4ED8;">${escapeHtml(a.matched_keyword)}</span>
            <span>${escapeHtml(a.publish_date)}</span>
            <span>${escapeHtml(a.org_name || "")}</span>
          </div>
        </div>
      </div>
    `;
    })
    .join("");
}

/* ==================== 视图 4：设置与 Webhook 测试 ==================== */
function readSettingsForm() {
  return {
    auto_start_enabled: $("set-autostart").checked,
    auto_schedule_time: ($("set-schedule-time")?.value || "22:30").trim(),
    backfill_days: Number($("set-backfill-days").value) || 250,
    notify_health_alerts: $("set-health-notify") ? $("set-health-notify").checked : true,
    notify_daily_summary: $("set-daily-summary").checked,
    notify_announcements: $("set-ann-notify").checked,
    auto_calibrate_thresholds: $("set-auto-calibrate").checked,
    alert_flow_yi: Number($("set-alert-flow").value) || 150,
    alert_score_min: Number($("set-alert-score").value) || 72,
    alert_active_lamps_min: Number($("set-alert-lamps").value) || 3,
    notify_win_toast: $("set-win-toast").checked,
    feishu_enabled: $("set-feishu-enabled").checked,
    feishu_webhook: $("set-feishu-webhook").value.trim(),
    feishu_secret: $("set-feishu-secret").value.trim(),
    wecom_enabled: $("set-wecom-enabled").checked,
    wecom_webhook: $("set-wecom-webhook").value.trim(),
    personal_wechat_enabled: $("set-pwx-enabled").checked,
    personal_wechat_provider: $("set-pwx-provider").value,
    personal_wechat_key: $("set-pwx-key").value.trim(),
    custom_webhook_enabled: $("set-custom-enabled").checked,
    custom_webhook_url: $("set-custom-url").value.trim(),
  };
}

async function loadSettings() {
  try {
    const [s, nh] = await Promise.all([apiGet("/api/settings"), apiGet("/api/notifications")]);
    $("set-autostart").checked = !!s.auto_start_enabled;
    if ($("set-schedule-time")) {
      $("set-schedule-time").value = s.auto_schedule_time || "22:30";
    }
    if ($("set-autostart-hint") && s.autostart_info) {
      const ai = s.autostart_info;
      $("set-autostart-hint").textContent = ai.enabled
        ? `已注册 Win11 任务计划（${ai.task_name}）：每日 ${ai.schedule_time || "22:30"} 执行，关机错过自动补跑${ai.next_run ? ` · 下次计划：${ai.next_run}` : ""}`
        : "写入 Win11 原生任务计划程序（免管理员权限），默认每晚 22:30 核实官方结论，错过自动开机补跑";
    }
    if ($("set-health-notify")) {
      $("set-health-notify").checked = s.notify_health_alerts !== false;
    }
    $("set-backfill-days").value = s.backfill_days ?? 250;
    $("set-daily-summary").checked = !!s.notify_daily_summary;
    $("set-ann-notify").checked = s.notify_announcements !== false;
    $("set-auto-calibrate").checked = s.auto_calibrate_thresholds !== false;
    $("set-alert-flow").value = s.alert_flow_yi ?? 150;
    $("set-alert-flow").disabled = $("set-auto-calibrate").checked;
    $("set-alert-score").value = s.alert_score_min ?? 72;
    if (s.current_scale) {
      const sc = s.current_scale;
      const modeTxt = sc.mode === "auto" ? "当前自动标尺" : "当前固定标尺";
      const pctNote = sc.pool_strong_aum_pct ? `，占全池 ${sc.pool_strong_aum_pct}%` : "";
      $("set-scale-live-hint").textContent = `${modeTxt}：核心12只 ±${sc.core_medium_yi}亿/±${sc.core_strong_yi}亿 ｜ 全池 ±${sc.medium_flow_yi}亿/±${sc.strong_flow_yi}亿（${pctNote.replace(/^，/, "")}） ｜ 弹窗线 ±${sc.alert_flow_yi}亿（总规模 ${sc.pool_aum_yi}亿）`;
    }
    $("set-alert-lamps").value = s.alert_active_lamps_min ?? 3;
    $("set-win-toast").checked = s.notify_win_toast !== false;
    $("set-feishu-enabled").checked = !!s.feishu_enabled;
    $("set-feishu-webhook").value = s.feishu_webhook || "";
    $("set-feishu-secret").value = s.feishu_secret || "";
    $("set-wecom-enabled").checked = !!s.wecom_enabled;
    $("set-wecom-webhook").value = s.wecom_webhook || "";
    $("set-pwx-enabled").checked = !!s.personal_wechat_enabled;
    $("set-pwx-provider").value = s.personal_wechat_provider || "serverchan";
    $("set-pwx-key").value = s.personal_wechat_key || "";
    $("set-custom-enabled").checked = !!s.custom_webhook_enabled;
    $("set-custom-url").value = s.custom_webhook_url || "";

    const tbody = $("notify-history-body");
    const items = nh.items || [];
    if (!items.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty-state">暂无提醒发送记录</td></tr>`;
    } else {
      tbody.innerHTML = items
        .map(
          (n) => `
          <tr>
            <td>${escapeHtml(n.sent_at)}</td>
            <td>${escapeHtml(n.trade_date)}</td>
            <td><span class="group-chip">${escapeHtml(n.stage)}</span></td>
            <td>${escapeHtml(n.title)}</td>
            <td>${escapeHtml((n.channels || []).join(", ") || "-")}</td>
          </tr>
        `
        )
        .join("");
    }
  } catch (e) {
    showToast("加载设置失败：" + e.message);
  }
}

async function saveSettings() {
  try {
    const payload = readSettingsForm();
    const res = await apiSend("/api/settings", "PUT", payload);
    const extra = [res.autostart_message, res.recompute_message].filter(Boolean).join(" · ");
    showToast(extra ? `设置已保存 · ${extra}` : "设置已保存");
    loadSettings();
    pollStatus();
  } catch (e) {
    showToast("保存设置失败：" + e.message, 4000);
  }
}

async function triggerSync(force = false) {
  try {
    const res = await apiSend("/api/sync", "POST", { force });
    showToast(res.message);
    pollStatus();
  } catch (e) {
    showToast("触发同步失败：" + e.message);
  }
}

/* ==================== 事件绑定 ==================== */
function bindEvents() {
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.onclick = () => switchView(btn.dataset.view);
  });

  $("btn-sync-now").onclick = () => triggerSync(false);
  $("btn-force-resync").onclick = () => triggerSync(true);
  $("btn-refresh-holdings").onclick = () => triggerSync(true);
  if ($("btn-health-resync")) $("btn-health-resync").onclick = () => triggerSync(true);

  $("overview-date-select").onchange = (e) => {
    state.selectedDate = e.target.value || null;
    loadDashboard();
  };

  $("trend-benchmark-select").onchange = (e) => {
    state.selectedBenchmark = e.target.value || "000300";
    syncScopeControls();
    loadDashboard();
  };

  $("trend-metric-tabs").querySelectorAll(".seg-btn").forEach((btn) => {
    btn.onclick = () => {
      state.flowMetric = btn.dataset.metric;
      $("trend-metric-tabs").querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("active", b === btn));
      if (state.dashboardData) {
        renderTrendChart(
          state.dashboardData.trend || [],
          state.dashboardData.benchmark_selected,
          state.dashboardData.flow_scope,
          state.dashboardData.groups_meta || [],
          state.dashboardData.missing_flow_days || 0
        );
      }
    };
  });

  $("trend-scope-tabs").querySelectorAll(".seg-btn").forEach((btn) => {
    btn.onclick = () => {
      if (btn.disabled) return;
      state.flowScope = btn.dataset.scope;
      syncScopeControls();
      loadDashboard();
    };
  });

  $("trend-range-tabs").querySelectorAll(".seg-btn").forEach((btn) => {
    btn.onclick = () => {
      state.trendDays = Number(btn.dataset.days);
      $("trend-range-tabs").querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("active", b === btn));
      loadDashboard();
    };
  });

  $("toggle-cum-line").onchange = () => {
    if (state.dashboardData) {
      renderTrendChart(
        state.dashboardData.trend || [],
        state.dashboardData.benchmark_selected,
        state.dashboardData.flow_scope,
        state.dashboardData.groups_meta || [],
        state.dashboardData.missing_flow_days || 0
      );
    }
  };

  // 日志筛选
  $("journal-verdict-pills").querySelectorAll(".pill").forEach((btn) => {
    btn.onclick = () => {
      state.journalVerdict = btn.dataset.verdict;
      $("journal-verdict-pills").querySelectorAll(".pill").forEach((b) => b.classList.toggle("active", b === btn));
      loadJournal();
    };
  });
  ["journal-start-date", "journal-end-date", "journal-only-notes"].forEach((id) => {
    $(id).onchange = loadJournal;
  });
  let kwTimer = null;
  $("journal-keyword").oninput = () => {
    clearTimeout(kwTimer);
    kwTimer = setTimeout(loadJournal, 250);
  };

  $("btn-export-md").onclick = () => {
    const q = buildJournalQueryParams();
    q.set("fmt", "markdown");
    window.open(`/api/logs/export?${q.toString()}`, "_blank");
  };
  $("btn-export-csv").onclick = () => {
    const q = buildJournalQueryParams();
    q.set("fmt", "csv");
    window.open(`/api/logs/export?${q.toString()}`, "_blank");
  };

  // 抽屉
  $("btn-close-drawer").onclick = closeLogDrawer;
  $("drawer-backdrop").onclick = closeLogDrawer;
  $("btn-save-note").onclick = saveDrawerNote;
  $("drawer-quick-tags").querySelectorAll(".qtag").forEach((btn) => {
    btn.onclick = () => {
      const inp = $("drawer-tags-input");
      const existing = inp.value
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const tag = btn.dataset.tag;
      if (!existing.includes(tag)) existing.push(tag);
      inp.value = existing.join(", ");
    };
  });

  // 口径说明弹窗
  $("btn-open-methodology").onclick = () => $("method-modal").classList.remove("hidden");
  $("btn-close-method").onclick = () => $("method-modal").classList.add("hidden");
  $("method-modal").onclick = (e) => {
    if (e.target === $("method-modal")) $("method-modal").classList.add("hidden");
  };

  // 设置与测试推送
  $("set-auto-calibrate").onchange = () => {
    $("set-alert-flow").disabled = $("set-auto-calibrate").checked;
  };
  $("btn-save-settings").onclick = saveSettings;
  document.querySelectorAll("[data-test-channel]").forEach((btn) => {
    btn.onclick = async () => {
      const ch = btn.dataset.testChannel;
      btn.disabled = true;
      try {
        const res = await apiSend("/api/settings/test-notify", "POST", {
          channel: ch,
          settings_override: readSettingsForm(),
        });
        showToast(res.ok ? `测试发送成功 (${ch})` : `发送失败：${res.message}`, 3500);
      } catch (e) {
        showToast(`发送失败：${e.message}`, 4000);
      } finally {
        btn.disabled = false;
      }
    };
  });

  window.addEventListener("resize", () => {
    Object.values(state.charts).forEach((c) => c && c.resize());
  });
}

window.addEventListener("DOMContentLoaded", () => {
  bindEvents();
  loadDashboard();
  pollStatus();
  setInterval(pollStatus, 2500);
});
