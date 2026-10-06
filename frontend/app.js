/* =====================================================================
 * 本地大模型实时性能监控测试平台 · 前端逻辑
 * 原生 JS + ECharts，无构建链，由 FastAPI 直接托管静态文件
 * ===================================================================== */
'use strict';

/* ------------------------------ 状态 ------------------------------ */
const S = {
  panel: 'dashboard',
  win: 60,
  paused: false,
  samples: [],
  connections: [],
  presets: [],
  models: {},           // connId -> string[]
  sysInfo: null,
  sampleInterval: 2,
  editingConnId: null,
  chat: { running: false, controller: null, startedAt: 0, chars: 0, buf: '' },
  bench: { runId: null, cases: [], stats: null },
  lt: { runId: null, series: [], stages: [], summary: null, report: null, running: false },
  hist: { runs: [], selected: new Set(), detail: null },
  alerts: { rules: null, list: [], notify: false },
  kpiHistory: { tps: [], ttft: [] },
};

/* ------------------------------ 工具 ------------------------------ */
const $ = (id) => document.getElementById(id);
const fmt = (v, digits = 1, dash = '—') =>
  (v === null || v === undefined || v === '' || Number.isNaN(v)) ? dash : Number(v).toFixed(digits);
const fmtInt = (v) => (v === null || v === undefined || v === '') ? '—' : Math.round(Number(v)).toLocaleString('zh-CN');
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const hhmmss = (ts) => {
  const d = new Date(ts * (ts > 1e12 ? 1 : 1000));
  return d.toLocaleTimeString('zh-CN', { hour12: false });
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/** 轻量 Markdown 渲染：代码块 / 行内代码 / 加粗 / 段落 */
function mdLite(text) {
  let t = esc(text);
  const blocks = [];
  t = t.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    blocks.push(`<pre data-lang="${lang}">${code.replace(/\n$/, '')}</pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  t = t.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  t = t.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  t = t.split(/\n{2,}/).map((p) => {
    if (/^\u0000\d+\u0000$/.test(p.trim())) return p;
    return `<p>${p.replace(/\n/g, '<br>')}</p>`;
  }).join('');
  return t.replace(/\u0000(\d+)\u0000/g, (m, i) => blocks[+i]);
}

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(path, opt);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const d = await res.json(); msg = d.detail || msg; } catch (e) { /* ignore */ }
    throw new Error(msg);
  }
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res.text();
}

/* ------------------------------ 提示与弹窗 ------------------------------ */
function toast(msg, kind = 'info', ms = 3600) {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.innerHTML = msg;
  $('toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, ms - 320);
  setTimeout(() => el.remove(), ms);
}
function logLine(tag, msg, kind = '') {
  const box = $('dashLog');
  if (!box) return;
  const row = document.createElement('div');
  row.innerHTML = `<span class="t">${hhmmss(Date.now() / 1000)}</span> <span class="${kind}">[${tag}]</span> ${esc(msg)}`;
  box.appendChild(row);
  box.scrollTop = box.scrollHeight;
  while (box.childElementCount > 300) box.removeChild(box.firstChild);
}
function openModal(title, html, copyText) {
  $('modalTitle').textContent = title;
  $('modalBody').innerHTML = html;
  $('modalMask').classList.add('show');
  $('modalCopy').dataset.copy = copyText || '';
}
$('modalClose').onclick = () => $('modalMask').classList.remove('show');
$('modalMask').onclick = (e) => { if (e.target === $('modalMask')) $('modalMask').classList.remove('show'); };
$('modalCopy').onclick = async () => {
  const t = $('modalCopy').dataset.copy || '';
  if (!t) return toast('没有可复制的内容', 'warn');
  try { await navigator.clipboard.writeText(t); toast('已复制到剪贴板', 'ok'); }
  catch (e) { toast('复制失败，请手动选择文本', 'err'); }
};

/* ------------------------------ 图表 ------------------------------ */
const charts = {};
const CH = {
  axis: {
    axisLine: { lineStyle: { color: '#30363d' } },
    axisLabel: { color: '#8b949e', fontSize: 10, hideOverlap: true },
    splitLine: { lineStyle: { color: '#21262d' } },
  },
  tooltip: {
    trigger: 'axis', backgroundColor: '#161b22', borderColor: '#30363d',
    textStyle: { color: '#e6edf3', fontSize: 11 },
    axisPointer: { type: 'cross', lineStyle: { color: '#484f58' }, crossStyle: { color: '#484f58' } },
  },
  // containLabel：把坐标轴标签算进绘图区，窄容器（看板左半屏）下不再挤压折线
  grid: { left: 8, right: 12, top: 34, bottom: 24, containLabel: true },
  legend: { textStyle: { color: '#8b949e', fontSize: 11 }, top: 2, itemWidth: 14, itemHeight: 8 },
};
function getChart(id) {
  if (!charts[id]) {
    const el = $(id);
    if (!el) return null;
    charts[id] = echarts.init(el, null, { renderer: 'canvas' });
  }
  return charts[id];
}
function setOpt(id, option, merge = true) {
  const c = getChart(id);
  if (!c) return null;
  c.setOption(option, merge);
  return c;
}
window.addEventListener('resize', () => {
  Object.keys(charts).forEach((k) => { try { charts[k].resize(); } catch (e) {} });
});
function resizePanelCharts(panel) {
  setTimeout(() => {
    document.querySelectorAll(`#panel-${panel} .chart`).forEach((el) => {
      const inst = echarts.getInstanceByDom(el);
      if (inst) inst.resize();
    });
  }, 40);
}
function timeAxes(unitFmt) {
  return {
    xAxis: { type: 'time', ...CH.axis, axisLabel: { color: '#8b949e', fontSize: 10, hideOverlap: true, formatter: '{HH}:{mm}:{ss}' } },
    tooltip: { ...CH.tooltip, valueFormatter: unitFmt },
  };
}
function areaGrad(color) {
  return new echarts.graphic.LinearGradient(0, 0, 0, 1, [
    { offset: 0, color: color + '66' }, { offset: 1, color: color + '05' },
  ]);
}

/* ------------------------------ WebSocket ------------------------------ */
let ws = null, wsRetry = 0, wsTimer = null;
function setWsStatus(state, text) {
  const el = $('wsStatus');
  el.classList.remove('on', 'off', 'connecting');
  el.classList.add(state);
  $('wsText').textContent = text;
  $('brandDot').style.background = state === 'on' ? 'var(--green)' : state === 'off' ? 'var(--red)' : 'var(--yellow)';
}
function connectWs() {
  setWsStatus('connecting', '连接中…');
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  try { ws = new WebSocket(`${proto}://${location.host}/ws`); } catch (e) { return scheduleReconnect(); }
  ws.onopen = () => {
    wsRetry = 0;
    setWsStatus('on', '实时连接');
    logLine('WS', '实时通道已建立', 'ok');
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    handleWsMessage(msg);
  };
  ws.onclose = () => { setWsStatus('off', '已断开'); scheduleReconnect(); };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}
function scheduleReconnect() {
  clearTimeout(wsTimer);
  const delay = Math.min(10000, 800 * Math.pow(1.6, wsRetry++));
  wsTimer = setTimeout(connectWs, delay);
}
function handleWsMessage(msg) {
  switch (msg.type) {
    case 'hello':
      S.sampleInterval = msg.data.sample_interval || 2;
      paintSampleBadge();
      if (msg.data.latest) applyMetrics(msg.data.latest);
      (msg.data.active_runs || []).forEach((r) => {
        if (r.run_type === 'loadtest') { S.lt.running = true; S.lt.runId = r.run_id; toggleLtButtons(true); }
        if (r.run_type === 'benchmark') { S.bench.runId = r.run_id; $('btnStopBench').disabled = false; }
        if (r.run_type === 'eval' && window.EVAL) { EVAL.onHello(r); }
      });
      break;
    case 'metrics': applyMetrics(msg.data); break;
    case 'metrics_history': (msg.data || []).forEach(applyMetrics); break;
    case 'progress': handleProgress(msg); break;
    case 'run_done': handleRunDone(msg); break;
    case 'stage_done': handleStageDone(msg); break;
    case 'eval_progress': if (window.EVAL) EVAL.onProgress(msg); break;
    case 'eval_done': if (window.EVAL) EVAL.onRunDone(msg); break;
    case 'connections_changed': refreshConnections(true); break;
    case 'alerts': { refreshAlerts(); toast('触发新告警，请查看告警面板', 'warn'); break; }
    default: break;
  }
}

/* ------------------------------ 1. 实时看板 ------------------------------ */
function applyMetrics(s) {
  if (!s || S.paused) { S.samples.push(s); trimSamples(); return; }
  S.samples.push(s);
  trimSamples();
  updateKpis(s);
  renderDashCharts();
}
function trimSamples() {
  const cutoff = Date.now() / 1000 - Math.max(S.win, 900) - 10;
  while (S.samples.length && (S.samples[0].ts || 0) < cutoff) S.samples.shift();
  while (S.samples.length > 4000) S.samples.shift();
}
function windowSamples() {
  const cutoff = Date.now() / 1000 - S.win;
  return S.samples.filter((s) => (s.ts || 0) >= cutoff);
}
function setKpi(id, value, sub, alarm) {
  const v = $(`v-${id}`), s = $(`s-${id}`), wrap = $(`kpi-${id}`);
  if (v) v.innerHTML = value;
  if (s) s.textContent = sub || '';
  if (wrap) wrap.classList.toggle('alarm', !!alarm);
}
function updateKpis(s) {
  const st = S.samples;
  // 累计徽标
  const lastWithStats = s.total_requests;
  $('badgeReqs').textContent = `累计请求 ${fmtInt(s.total_requests)}`;
  $('badgeTokens').textContent = `累计 Token ${fmtInt(s.total_tokens)}`;
  $('badgeErrors').textContent = `累计错误 ${fmtInt(s.error_requests)}`;

  const tps = s.current_tps || 0;
  const ttft = s.current_ttft_ms || 0;
  setKpi('tps', `${fmt(tps, 1)}<span class="unit">tok/s</span>`,
    tps > 0 ? '实时平滑值' : '等待推理请求', tps > 0 && tps < 5);
  setKpi('ttft', `${fmt(ttft, 0)}<span class="unit">ms</span>`,
    ttft > 3000 ? '已超过 3s' : '平滑均值', ttft > 3000);
  setKpi('e2e', `${fmt(s.last_e2e_ms, 0)}<span class="unit">ms</span>`, '最近一次请求', false);
  setKpi('active', `${s.active_reqs || 0}<span class="unit">req</span>`,
    `RPS ${fmt(s.rps, 2)}`, false);

  const vramPct = (s.vram_used_mb && s.vram_total_mb) ? s.vram_used_mb / s.vram_total_mb * 100 : null;
  setKpi('gpu', s.gpu_util === null || s.gpu_util === undefined ? '—' : `${fmt(s.gpu_util, 1)}<span class="unit">%</span>`,
    s.gpu_name || '未检测到 GPU', false);
  setKpi('vram', vramPct === null ? '—' : `${fmt(vramPct, 1)}<span class="unit">%</span>`,
    vramPct === null ? '无显存数据' :
      `${fmt(s.vram_used_mb / 1024, 1)} / ${fmt(s.vram_total_mb / 1024, 1)} GiB`,
    vramPct > 90);
  setKpi('cpu', `${fmt(s.cpu_percent, 1)}<span class="unit">%</span>`,
    s.proc_rss_mb ? `推理进程 ${fmt(s.proc_rss_mb, 0)} MB` : '未匹配到推理进程', false);
  setKpi('ram', `${fmt(s.ram_percent, 1)}<span class="unit">%</span>`,
    s.ram_used_mb ? `已用 ${fmt(s.ram_used_mb / 1024, 1)} GiB` : '', false);

  // GPU 模拟标识
  if (s.simulated) {
    const b = $('gpuBadge');
    b.style.display = '';
    b.textContent = 'GPU 模拟数据';
    b.style.color = 'var(--yellow)';
  }
}
function renderDashCharts() {
  if (S.panel !== 'dashboard') return;
  const data = windowSamples();
  if (!data.length) return;
  const tpsData = data.map((s) => [s.ts * 1000, s.current_tps || 0]);
  const e2eData = data.map((s) => [s.ts * 1000, s.last_e2e_ms || 0]);
  setOpt('chart-tps', {
    ...CH, legend: { ...CH.legend, data: ['生成速度 TPS', '端到端延迟'] },
    xAxis: { type: 'time', ...CH.axis, axisLabel: { color: '#8b949e', fontSize: 10, hideOverlap: true, formatter: '{HH}:{mm}:{ss}' } },
    yAxis: [
      { type: 'value', name: 'TPS', nameTextStyle: { color: '#3fb950', fontSize: 10 }, ...CH.axis },
      { type: 'value', name: 'ms', nameTextStyle: { color: '#58a6ff', fontSize: 10 }, ...CH.axis, splitLine: { show: false } },
    ],
    series: [
      { name: '生成速度 TPS', type: 'line', showSymbol: false, smooth: true, data: tpsData,
        lineStyle: { width: 2, color: '#3fb950' }, itemStyle: { color: '#3fb950' },
        areaStyle: { color: areaGrad('#3fb950') } },
      { name: '端到端延迟', type: 'line', yAxisIndex: 1, showSymbol: false, smooth: true, data: e2eData,
        lineStyle: { width: 2, color: '#58a6ff', type: 'dashed' }, itemStyle: { color: '#58a6ff' } },
    ],
  });

  const util = data.map((s) => [s.ts * 1000, s.gpu_util]);
  const vram = data.map((s) => [s.ts * 1000, s.vram_used_mb ? s.vram_used_mb / 1024 : null]);
  setOpt('chart-gpu', {
    ...CH, legend: { ...CH.legend, data: ['GPU 利用率', '显存占用'] },
    xAxis: { type: 'time', ...CH.axis, axisLabel: { color: '#8b949e', fontSize: 10, hideOverlap: true, formatter: '{HH}:{mm}:{ss}' } },
    yAxis: [
      { type: 'value', name: '%', max: 100, nameTextStyle: { color: '#3fb950', fontSize: 10 }, ...CH.axis },
      { type: 'value', name: 'GiB', nameTextStyle: { color: '#bc8cff', fontSize: 10 }, ...CH.axis, splitLine: { show: false } },
    ],
    series: [
      { name: 'GPU 利用率', type: 'line', showSymbol: false, smooth: true, data: util,
        lineStyle: { width: 2, color: '#3fb950' }, areaStyle: { color: areaGrad('#3fb950') } },
      { name: '显存占用', type: 'line', yAxisIndex: 1, showSymbol: false, smooth: true, data: vram,
        lineStyle: { width: 2, color: '#bc8cff' }, areaStyle: { color: areaGrad('#bc8cff') } },
    ],
  });

  const cpu = data.map((s) => [s.ts * 1000, s.cpu_percent]);
  const ram = data.map((s) => [s.ts * 1000, s.ram_percent]);
  const rss = data.map((s) => [s.ts * 1000, s.proc_rss_mb || null]);
  setOpt('chart-cpu', {
    ...CH, legend: { ...CH.legend, data: ['CPU %', '内存 %', '推理进程 RSS'] },
    xAxis: { type: 'time', ...CH.axis, axisLabel: { color: '#8b949e', fontSize: 10, hideOverlap: true, formatter: '{HH}:{mm}:{ss}' } },
    yAxis: [
      { type: 'value', name: '%', max: 100, nameTextStyle: { color: '#8b949e', fontSize: 10 }, ...CH.axis },
      { type: 'value', name: 'MB', nameTextStyle: { color: '#d29922', fontSize: 10 }, ...CH.axis, splitLine: { show: false } },
    ],
    series: [
      { name: 'CPU %', type: 'line', showSymbol: false, smooth: true, data: cpu, lineStyle: { width: 2, color: '#58a6ff' } },
      { name: '内存 %', type: 'line', showSymbol: false, smooth: true, data: ram, lineStyle: { width: 2, color: '#39c5cf' } },
      { name: '推理进程 RSS', type: 'line', yAxisIndex: 1, showSymbol: false, smooth: true, data: rss,
        lineStyle: { width: 2, color: '#d29922', type: 'dashed' } },
    ],
  });
}
async function loadSystemInfo() {
  try {
    const info = await api('GET', '/api/system/info');
    S.sysInfo = info;
    const g = info.gpu;
    $('hostInfo').textContent = `${info.os.hostname} · ${info.os.platform}`;
    $('gpuBadge').style.display = '';
    $('gpuBadge').textContent = g.gpu_available
      ? `${g.gpu_names[0]} · ${fmt(g.vram_total_mb / 1024, 1)} GiB`
      : (g.simulated ? 'GPU 模拟数据（未检测到 NVIDIA 设备）' : '未检测到 GPU');
    $('gpuBadge').style.color = g.gpu_available ? 'var(--green)' : 'var(--yellow)';
    $('sysInfo').innerHTML = [
      ['GPU', g.gpu_names.join(' / ') || '未检测到'],
      ['显存总量', g.vram_total_mb ? `${fmt(g.vram_total_mb, 0)} MB（${fmt(g.vram_total_mb / 1024, 1)} GiB）` : '—'],
      ['CPU', `${info.cpu.name}`],
      ['核心数', `物理 ${info.cpu.cores_physical} / 逻辑 ${info.cpu.cores_logical}`],
      ['主频', info.cpu.freq_ghz ? `${info.cpu.freq_ghz} GHz` : '—'],
      ['内存总量', `${fmt(info.ram.total_mb / 1024, 1)} GiB`],
      ['操作系统', info.os.platform],
      ['Python', info.os.python],
      ['主机名', info.os.hostname],
    ].map(([k, v]) => `<div class="metric-item"><span class="k">${esc(k)}</span><span class="v" style="font-size:12px;font-weight:500">${esc(v)}</span></div>`).join('');
  } catch (e) { logLine('SYS', `系统信息获取失败: ${e.message}`, 'err'); }
}
async function refreshNativeMetrics() {
  const cid = S.connections[0] ? S.connections[0].id : null;
  const box = $('nativeMetrics');
  if (!cid) { box.innerHTML = '<div class="empty">暂无连接档案</div>'; return; }
  try {
    const r = await api('GET', `/api/models?conn_id=${cid}`);
    const n = r.native || {};
    const rows = [];
    if (n.num_requests_running !== undefined) rows.push(['运行中请求数', n.num_requests_running]);
    if (n.num_requests_waiting !== undefined) rows.push(['排队请求数', n.num_requests_waiting]);
    if (n.gpu_cache_usage_perc !== undefined) rows.push(['KV Cache 占用', fmt(n.gpu_cache_usage_perc * 100, 1) + '%']);
    if (n.avg_generation_throughput !== undefined) rows.push(['服务端平均吞吐', fmt(n.avg_generation_throughput, 1) + ' tok/s']);
    if (n.prompt_tokens_total !== undefined) rows.push(['累计 Prompt Token', fmtInt(n.prompt_tokens_total)]);
    if (n.generation_tokens_total !== undefined) rows.push(['累计生成 Token', fmtInt(n.generation_tokens_total)]);
    if (n.request_success_total !== undefined) rows.push(['累计成功请求', fmtInt(n.request_success_total)]);
    if (n.loaded_vram_mb !== undefined) rows.push(['已加载模型显存', fmt(n.loaded_vram_mb, 0) + ' MB']);
    if (n.loaded_models && n.loaded_models.length) {
      rows.push(['已加载模型', n.loaded_models.map((m) => m.name).join(', ')]);
    }
    if (n.note) rows.push(['说明', n.note]);
    $('nativeHint').textContent = `连接 #${cid} · ${r.models.length} 个模型`;
    box.innerHTML = rows.length
      ? rows.map(([k, v]) => `<div class="metric-item"><span class="k">${esc(k)}</span><span class="v" style="font-size:12px">${esc(v)}</span></div>`).join('')
      : '<div class="empty">该后端未提供原生运行指标</div>';
  } catch (e) {
    box.innerHTML = `<div class="empty">拉取失败：${esc(e.message)}</div>`;
  }
}

/* ------------------------------ 2. 连接管理 ------------------------------ */
async function loadPresets() {
  S.presets = await api('GET', '/api/backends/presets');
  $('connPreset').innerHTML = '<option value="">— 选择预设自动填充 —</option>' +
    S.presets.map((p) => `<option value="${p.backend}">${esc(p.label)}</option>`).join('');
}
$('connPreset').onchange = () => {
  const p = S.presets.find((x) => x.backend === $('connPreset').value);
  if (!p) return;
  $('connBackend').value = p.backend;
  $('connUrl').value = p.base_url;
  if (!$('connName').value) $('connName').value = p.label;
  toast(p.hint, 'info', 5200);
};
$('connBackend').onchange = () => {
  const p = S.presets.find((x) => x.backend === $('connBackend').value);
  if (p && !$('connUrl').value) $('connUrl').value = p.base_url;
};

async function refreshConnections(silent) {
  try {
    S.connections = await api('GET', '/api/connections');
    renderConnTable();
    renderConnSelects();
    chatRefreshModels();
    if (!silent) logLine('CONN', `已加载 ${S.connections.length} 个连接档案`, 'ok');
  } catch (e) { toast(`连接列表加载失败：${e.message}`, 'err'); }
}
const STATUS_TEXT = { online: '在线', offline: '离线', timeout: '超时', unknown: '未知' };
function renderConnTable() {
  const tb = $('connTable');
  if (!S.connections.length) { tb.innerHTML = '<tr><td colspan="7" class="empty">还没有连接档案，请在右侧新增。</td></tr>'; return; }
  tb.innerHTML = S.connections.map((c) => `
    <tr>
      <td><span class="led-dot ${c.last_status}" title="${STATUS_TEXT[c.last_status] || ''}"></span>
        <span class="small muted">${STATUS_TEXT[c.last_status] || ''}</span></td>
      <td><strong>${esc(c.name)}</strong></td>
      <td><span class="badge blue">${esc(c.backend)}</span></td>
      <td class="mono small">${esc(c.base_url)}</td>
      <td class="small">${esc(c.default_model) || '<span class="muted">—</span>'}</td>
      <td class="small muted">${esc(c.last_version) || '—'}</td>
      <td class="nowrap">
        <button class="btn sm" data-act="probe" data-id="${c.id}">测试</button>
        <button class="btn sm" data-act="edit" data-id="${c.id}">编辑</button>
        <button class="btn sm danger" data-act="del" data-id="${c.id}">删除</button>
      </td>
    </tr>`).join('');
}
$('connTable').onclick = async (e) => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  const id = Number(btn.dataset.id);
  const conn = S.connections.find((c) => c.id === id);
  if (btn.dataset.act === 'edit') {
    S.editingConnId = id;
    $('connFormTitle').textContent = `编辑连接档案 #${id}`;
    $('connName').value = conn.name; $('connBackend').value = conn.backend;
    $('connUrl').value = conn.base_url; $('connKey').value = conn.api_key || '';
    $('connModel').value = conn.default_model || '';
    toast('已载入表单，修改后点击「保存档案」', 'info');
  } else if (btn.dataset.act === 'del') {
    if (!confirm(`确认删除连接档案「${conn.name}」？该操作不影响已保存的测试记录。`)) return;
    try { await api('DELETE', `/api/connections/${id}`); toast('已删除', 'ok'); refreshConnections(); }
    catch (err) { toast(err.message, 'err'); }
  } else if (btn.dataset.act === 'probe') {
    btn.innerHTML = '<span class="spin"></span>';
    try {
      const r = await api('POST', `/api/connections/${id}/test`);
      renderProbeResult(r);
      toast(r.ok ? `连接成功 · ${r.models.length} 个模型 · ${fmt(r.latency_ms, 0)} ms`
                 : `连接失败：${r.error}`, r.ok ? 'ok' : 'err');
      refreshConnections(true);
    } catch (err) { toast(err.message, 'err'); }
    btn.innerHTML = '测试';
  }
};
function renderProbeResult(r) {
  const box = $('connProbeResult');
  if (!r) { box.innerHTML = ''; return; }
  if (!r.ok) {
    box.innerHTML = `<div class="card" style="border-color:var(--red)"><b style="color:var(--red)">连接失败</b>
      <div class="small mt8 mono">${esc(r.error || '')}</div></div>`;
    return;
  }
  box.innerHTML = `<div class="card" style="border-color:var(--green)">
    <b style="color:var(--green)">连接成功</b>
    <div class="small muted mt8">版本：<span class="mono">${esc(r.version || '—')}</span> · 探测耗时 ${fmt(r.latency_ms, 0)} ms</div>
    <div class="small muted mt8">可用模型（${r.models.length}）：</div>
    <div class="mt8" style="display:flex;gap:6px;flex-wrap:wrap">
      ${r.models.slice(0, 40).map((m) => `<span class="badge">${esc(m)}</span>`).join('') || '<span class="muted small">无</span>'}
    </div></div>`;
}
$('btnRefreshConns').onclick = () => refreshConnections();
$('btnResetConn').onclick = () => {
  S.editingConnId = null;
  $('connFormTitle').textContent = '新增连接档案';
  ['connName', 'connUrl', 'connKey', 'connModel'].forEach((k) => { $(k).value = ''; });
  $('connPreset').value = ''; $('connBackend').value = 'demo';
  $('connProbeResult').innerHTML = '';
};
$('btnSaveConn').onclick = async () => {
  const payload = {
    name: $('connName').value.trim(), backend: $('connBackend').value,
    base_url: $('connUrl').value.trim(), api_key: $('connKey').value.trim(),
    default_model: $('connModel').value.trim(),
  };
  if (!payload.name || !payload.base_url) return toast('名称与 Base URL 必填', 'warn');
  try {
    if (S.editingConnId) { await api('PUT', `/api/connections/${S.editingConnId}`, payload); toast('已更新', 'ok'); }
    else { await api('POST', '/api/connections', payload); toast('已新增', 'ok'); }
    $('btnResetConn').click();
    await refreshConnections();
  } catch (e) { toast(e.message, 'err'); }
};
$('btnProbeConn').onclick = async () => {
  const payload = { backend: $('connBackend').value, base_url: $('connUrl').value.trim(), api_key: $('connKey').value.trim() };
  if (!payload.base_url) return toast('请填写 Base URL', 'warn');
  const btn = $('btnProbeConn');
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span> 探测中';
  try {
    const r = await api('POST', '/api/connections/probe', payload);
    renderProbeResult(r);
    if (r.ok && r.models.length) {
      const p = S.presets.find((x) => x.backend === payload.backend);
      if (!$('connName').value) $('connName').value = p ? p.label : payload.base_url;
      if (!$('connModel').value) $('connModel').value = r.models[0];
    }
    toast(r.ok ? `连接成功，拉到 ${r.models.length} 个模型` : `连接失败：${r.error}`, r.ok ? 'ok' : 'err');
  } catch (e) { toast(e.message, 'err'); }
  btn.disabled = false; btn.innerHTML = '测试连接';
};

/* ------------------------------ 选择器渲染 ------------------------------ */
function connOptions(selected) {
  return '<option value="">— 请选择连接 —</option>' + S.connections.map((c) =>
    `<option value="${c.id}" ${selected === c.id ? 'selected' : ''}>#${c.id} ${esc(c.name)}（${esc(c.backend)}）</option>`).join('');
}
function renderConnSelects() {
  const keep = (id) => $(id) ? $(id).value : null;
  const CONNS = ['chatConn', 'benchConn', 'ltConn', 'evConn'];
  const prev = {};
  CONNS.forEach((id) => { prev[id] = keep(id); });
  const prevJudge = keep('evJudge');
  CONNS.forEach((id) => { if ($(id)) $(id).innerHTML = connOptions(); });
  const first = S.connections[0];
  const sel = (id, prevVal) => {
    if (!$(id)) return;
    $(id).value = prevVal && S.connections.some((c) => String(c.id) === String(prevVal))
      ? prevVal : (first ? String(first.id) : '');
  };
  CONNS.forEach((id) => sel(id, prev[id]));
  // 裁判模型是可选的，多一个"不使用"空选项
  if ($('evJudge')) {
    $('evJudge').innerHTML = '<option value="">— 不使用裁判模型 —</option>' + S.connections
      .map((c) => `<option value="${c.id}">#${c.id} ${esc(c.name)}（${esc(c.backend)}）</option>`).join('');
    $('evJudge').value = (prevJudge && S.connections.some((c) => String(c.id) === String(prevJudge)))
      ? prevJudge : '';
  }
  ['chatConn', 'benchConn', 'ltConn'].forEach((id) => {
    if ($(id)) loadModelsInto(id.replace('Conn', 'Model'), $(id).value);
  });
  if ($('evConn') && $('evConn').value) loadModelsInto('evModel', $('evConn').value);
}
async function loadModelsInto(modelSelectId, connId) {
  const el = $(modelSelectId);
  if (!el) return;
  if (!connId) { el.innerHTML = '<option value="">—</option>'; return; }
  const conn = S.connections.find((c) => String(c.id) === String(connId));
  el.innerHTML = '<option value="">加载中…</option>';
  try {
    if (!S.models[connId]) {
      const r = await api('GET', `/api/models?conn_id=${connId}`);
      S.models[connId] = r.models || [];
    }
    const list = S.models[connId];
    el.innerHTML = list.length
      ? list.map((m) => `<option value="${esc(m)}" ${conn && conn.default_model === m ? 'selected' : ''}>${esc(m)}</option>`).join('')
      : `<option value="${esc(conn ? conn.default_model : '')}">${esc(conn && conn.default_model ? conn.default_model : '该服务未返回模型列表')}</option>`;
  } catch (e) {
    el.innerHTML = `<option value="${esc(conn ? conn.default_model || '' : '')}">${esc(conn && conn.default_model ? conn.default_model : '拉取失败，可手填')}</option>`;
    el.title = e.message;
  }
}
['chatConn', 'benchConn', 'ltConn'].forEach((id) => {
  document.addEventListener('DOMContentLoaded', () => {});
});

/* ------------------------------ 3. 对话测试（多轮 + 记忆） ------------------------------
 * 实现全部搬到 frontend/chat.js（window.CHAT）：
 * 会话列表、多轮消息、长期记忆、上下文预算、继续生成/重新生成。
 * 这里只保留与全局状态相关的一小段桥接。
 * -------------------------------------------------------------------------------------- */
function chatRefreshModels() {
  if (window.CHAT && typeof CHAT.onConnectionsChanged === 'function') CHAT.onConnectionsChanged();
}

/* ------------------------------ 4. 基准测试 ------------------------------ */
let SUITES = [];
async function loadSuites() {
  const r = await api('GET', '/api/test/suites');
  SUITES = r.suites;
  $('benchSuite').innerHTML = SUITES.map((s) =>
    `<option value="${s.id}">${esc(s.name)}（${s.case_count} 条）</option>`).join('');
  updateSuiteDesc();
}
function updateSuiteDesc() {
  const s = SUITES.find((x) => x.id === $('benchSuite').value);
  $('benchSuiteDesc').textContent = s ? `${s.desc} 用例：${s.cases.map((c) => c.label).join(' / ')}` : '';
}
$('benchSuite').onchange = updateSuiteDesc;

$('btnRunBench').onclick = async () => {
  const connId = Number($('benchConn').value);
  if (!connId) return toast('请选择连接档案', 'warn');
  const overrides = {};
  if (Number($('benchMaxTokens').value) > 0) overrides.max_tokens = Number($('benchMaxTokens').value);
  if ($('benchTemp').value !== '') overrides.temperature = Number($('benchTemp').value);
  $('btnRunBench').disabled = true; $('btnStopBench').disabled = false;
  $('benchCaseTable').innerHTML = '<tr><td colspan="9" class="empty">等待首个用例完成…</td></tr>';
  S.bench.cases = []; S.bench.stats = null;
  try {
    const r = await api('POST', '/api/test/benchmark', {
      conn_id: connId, model: $('benchModel').value || '',
      suite_id: $('benchSuite').value, overrides,
    });
    S.bench.runId = r.run_id;
    toast(`基准套件已启动，Run #${r.run_id}`, 'ok');
    logLine('BENCH', `启动套件 ${$('benchSuite').value}，Run #${r.run_id}`, 'info');
  } catch (e) {
    toast(e.message, 'err');
    $('btnRunBench').disabled = false; $('btnStopBench').disabled = true;
  }
};
$('btnStopBench').onclick = async () => {
  if (!S.bench.runId) return;
  try { await api('POST', '/api/test/stop', { run_id: S.bench.runId }); toast('已请求停止', 'warn'); }
  catch (e) { toast(e.message, 'err'); }
};
function benchProgress(p) {
  const pct = p.total ? (p.done / p.total * 100) : 0;
  $('benchProgress').innerHTML = `
    <div class="small muted">进度 <b class="mono">${p.done}</b> / ${p.total} 用例 · 已耗时 ${fmt(p.elapsed_ms / 1000, 1)} s</div>
    <div class="bar-outer mt8"><div class="bar-inner" style="width:${clamp(pct, 0, 100)}%"></div></div>
    <div class="small muted mt8">当前用例：${esc(p.stage_index + 1)} / ${p.stage_count}</div>`;
}
function renderBenchResults(cases, stats, box) {
  // 用例表
  $('benchCaseTable').innerHTML = cases.length ? cases.map((c) => `
    <tr><td><strong>${esc(c.label)}</strong></td>
      <td><span class="badge">${esc(c.category || '—')}</span></td>
      <td class="num">${c.context_tokens ? fmtInt(c.context_tokens) : '—'}</td>
      <td class="num">${fmtInt(c.prompt_tokens)}</td>
      <td class="num">${fmtInt(c.completion_tokens)}</td>
      <td class="num">${fmt(c.ttft_ms, 1)}</td>
      <td class="num">${fmt(c.e2e_ms, 0)}</td>
      <td class="num"><b>${fmt(c.tps, 2)}</b></td>
      <td>${c.status === 'ok' ? '<span class="badge green">ok</span>' : `<span class="badge red" title="${esc(c.error || '')}">error</span>`}</td>
    </tr>`).join('') : '<tr><td colspan="9" class="empty">无数据</td></tr>';

  // 箱线图
  const mk = (b) => b && b.min !== null ? [b.min, b.q1, b.median, b.q3, b.max] : null;
  if (box) {
    const rows = [['TTFT (ms)', mk(box.ttft)], ['E2E (ms)', mk(box.e2e)], ['TPS', mk(box.tps)]]
      .filter((r) => r[1]);
    setOpt('chart-bench-box', {
      ...CH, tooltip: { ...CH.tooltip, trigger: 'item' },
      grid: { left: 66, right: 24, top: 18, bottom: 26 },
      xAxis: { type: 'value', ...CH.axis, scale: true },
      yAxis: { type: 'category', data: rows.map((r) => r[0]), ...CH.axis, splitLine: { show: false } },
      series: [{ type: 'boxplot', data: rows.map((r) => r[1]), itemStyle: { color: '#1f6feb', borderColor: '#58a6ff' },
        boxWidth: [12, 34] }],
    }, false);
  }
  // 用例柱线图
  const labels = cases.map((c) => c.label.replace(/^[^·]*·\s*/, ''));
  setOpt('chart-bench-bar', {
    ...CH, legend: { ...CH.legend, data: ['TPS', 'TTFT'] },
    grid: { left: 46, right: 46, top: 30, bottom: 62 },
    xAxis: { type: 'category', data: labels, ...CH.axis, axisLabel: { color: '#8b949e', fontSize: 9, rotate: 32, interval: 0 } },
    yAxis: [
      { type: 'value', name: 'TPS', nameTextStyle: { color: '#3fb950', fontSize: 10 }, ...CH.axis },
      { type: 'value', name: 'ms', nameTextStyle: { color: '#58a6ff', fontSize: 10 }, ...CH.axis, splitLine: { show: false } },
    ],
    series: [
      { name: 'TPS', type: 'bar', data: cases.map((c) => c.tps), itemStyle: { color: '#238636', borderRadius: [3, 3, 0, 0] }, barMaxWidth: 26 },
      { name: 'TTFT', type: 'line', yAxisIndex: 1, data: cases.map((c) => c.ttft_ms), lineStyle: { color: '#58a6ff', width: 2 }, itemStyle: { color: '#58a6ff' } },
    ],
  }, false);

  // 汇总
  if (stats) {
    $('benchSummary').innerHTML = [
      ['请求总数', `${stats.total}（成功 ${stats.ok} / 失败 ${stats.errors}）`],
      ['平均 TPS', `${fmt(stats.avg_tps, 2)} tok/s（最小 ${fmt(stats.min_tps, 1)} / 最大 ${fmt(stats.max_tps, 1)}）`],
      ['平均 TTFT', `${fmt(stats.avg_ttft_ms, 1)} ms`],
      ['TTFT P50 / P90 / P99', `${fmt(stats.p50_ttft_ms, 1)} / ${fmt(stats.p90_ttft_ms, 1)} / ${fmt(stats.p99_ttft_ms, 1)} ms`],
      ['平均 E2E', `${fmt(stats.avg_e2e_ms, 0)} ms`],
      ['E2E P50 / P90 / P99', `${fmt(stats.p50_e2e_ms, 0)} / ${fmt(stats.p90_e2e_ms, 0)} / ${fmt(stats.p99_e2e_ms, 0)} ms`],
      ['Token 总量', fmtInt(stats.total_tokens)],
      ['总耗时', `${fmt(stats.duration_ms / 1000, 1)} s`],
    ].map(([k, v]) => `<div class="metric-item"><span class="k">${k}</span><span class="v" style="font-size:12px">${v}</span></div>`).join('');
  }
}

/* ------------------------------ 5. 并发压测 ------------------------------ */
$('ltMode').onchange = () => {
  const stage = $('ltMode').value === 'stage';
  $('ltStageBox').style.display = stage ? '' : 'none';
  $('ltFixedBox').style.display = stage ? 'none' : '';
  $('ltDurLabel').textContent = stage ? '总时长上限（秒，0 表示按阶梯自然结束）' : '持续时长（秒）';
  updateLtPlan();
};
function updateLtPlan() {
  const mode = $('ltMode').value;
  let plan = [];
  if (mode === 'stage') {
    let c = Math.max(1, Number($('ltStartC').value) || 1);
    const maxC = Math.max(c, Number($('ltMaxC').value) || 16);
    const secs = Math.max(5, Number($('ltStageSec').value) || 30);
    const limit = Number($('ltDuration').value) || 0;
    let acc = 0;
    while (c <= maxC) {
      if (limit > 0 && acc >= limit) break;
      const take = limit > 0 ? Math.min(secs, limit - acc) : secs;
      plan.push([c, take]); acc += take; c *= 2;
    }
  } else {
    plan = [[Math.max(1, Number($('ltConcurrency').value) || 8), Math.max(5, Number($('ltDuration').value) || 60)]];
  }
  $('ltPlan').innerHTML = `阶梯计划：${plan.map(([c, s]) => `<span class="badge blue">${c} 并发 × ${s}s</span>`).join(' → ')}` +
    `　合计约 ${plan.reduce((a, b) => a + b[1], 0)} 秒`;
  return plan;
}
['ltConcurrency', 'ltStartC', 'ltMaxC', 'ltStageSec', 'ltDuration'].forEach((id) => {
  const el = $(id); if (el) el.oninput = updateLtPlan;
});
function toggleLtButtons(running) {
  $('btnStartLt').disabled = running;
  $('btnStopLt').disabled = !running;
  $('btnStartLt').innerHTML = running ? '<span class="spin"></span> 压测进行中' : '启动压测';
}
$('btnStartLt').onclick = async () => {
  const connId = Number($('ltConn').value);
  if (!connId) return toast('请选择连接档案', 'warn');
  const mode = $('ltMode').value;
  const prompts = $('ltPrompts').value.split('\n').map((s) => s.trim()).filter(Boolean);
  if (!prompts.length) return toast('Prompt 池至少需要一条', 'warn');
  const payload = {
    conn_id: connId, model: $('ltModel').value || '', prompts, mode,
    concurrency: Math.max(1, Number($('ltConcurrency').value) || 8),
    duration: Math.max(0, Number($('ltDuration').value) || 0),
    start_concurrency: Math.max(1, Number($('ltStartC').value) || 1),
    max_concurrency: Math.max(1, Number($('ltMaxC').value) || 16),
    stage_seconds: Math.max(5, Number($('ltStageSec').value) || 30),
    max_tokens: Number($('ltMaxTokens').value) || 256,
    temperature: Number($('ltTemp').value) || 0.7,
  };
  S.lt.series = []; S.lt.stages = []; S.lt.summary = null; S.lt.report = null;
  $('ltStageTable').innerHTML = '<tr><td colspan="10" class="empty">压测进行中…</td></tr>';
  $('ltReport').innerHTML = '<div class="empty">压测结束后生成摘要</div>';
  try {
    const r = await api('POST', '/api/test/loadtest/start', payload);
    S.lt.runId = r.run_id; S.lt.running = true;
    toggleLtButtons(true);
    toast(`压测已启动，Run #${r.run_id}`, 'ok');
    logLine('LOAD', `启动压测 Run #${r.run_id}，模式 ${mode}`, 'info');
  } catch (e) { toast(e.message, 'err'); toggleLtButtons(false); }
};
$('btnStopLt').onclick = async () => {
  if (!S.lt.runId) return;
  try { await api('POST', '/api/test/stop', { run_id: S.lt.runId }); toast('已请求停止压测', 'warn'); }
  catch (e) { toast(e.message, 'err'); }
};
function ltPush(p) {
  S.lt.series.push({
    t: Date.now(),
    qps: p.current_qps || 0, avg: p.avg_latency_ms || 0, p99: p.p99_latency_ms || 0,
    err: (p.error_rate || 0) * 100, conc: p.concurrency || 0,
    gpu: p.gpu_util, vram: p.vram_used_mb && S.sysInfo ? p.vram_used_mb / 1024 : null,
  });
  if (S.lt.series.length > 900) S.lt.series.shift();
  $('lt-qps').textContent = fmt(p.current_qps, 2);
  $('lt-avg').innerHTML = `${fmt(p.avg_latency_ms, 0)}<span class="unit">ms</span>`;
  $('lt-p99').innerHTML = `${fmt(p.p99_latency_ms, 0)}<span class="unit">ms</span>`;
  $('lt-err').innerHTML = `${fmt((p.error_rate || 0) * 100, 2)}<span class="unit">%</span>`;
  $('lt-done').textContent = `已完成 ${p.done} · 成功 ${p.ok} · 失败 ${p.errors}`;
  $('lt-qpsSub').textContent = `并发 ${p.concurrency} · 阶段 ${p.stage_index + 1}/${p.stage_count}`;
  const box = document.querySelector('#kpi-lt-err');
  renderLtCharts();
}
function renderLtCharts() {
  if (S.panel !== 'loadtest') return;
  const d = S.lt.series;
  if (!d.length) return;
  const x = d.map((p) => p.t);
  setOpt('chart-lt-qps', {
    ...CH, legend: { ...CH.legend, data: ['QPS', '并发数'] },
    xAxis: { type: 'time', ...CH.axis, axisLabel: { color: '#8b949e', fontSize: 9, hideOverlap: true, formatter: '{HH}:{mm}:{ss}' } },
    yAxis: [
      { type: 'value', name: 'QPS', nameTextStyle: { color: '#3fb950', fontSize: 10 }, ...CH.axis },
      { type: 'value', name: '并发', nameTextStyle: { color: '#bc8cff', fontSize: 10 }, ...CH.axis, splitLine: { show: false } },
    ],
    series: [
      { name: 'QPS', type: 'line', showSymbol: false, smooth: true, data: d.map((p, i) => [x[i], p.qps]),
        lineStyle: { width: 2, color: '#3fb950' }, areaStyle: { color: areaGrad('#3fb950') } },
      { name: '并发数', type: 'line', yAxisIndex: 1, step: 'end', showSymbol: false, data: d.map((p, i) => [x[i], p.conc]),
        lineStyle: { width: 1.5, color: '#bc8cff', type: 'dashed' } },
    ],
  });
  setOpt('chart-lt-lat', {
    ...CH, legend: { ...CH.legend, data: ['平均延迟', 'P99 延迟'] },
    xAxis: { type: 'time', ...CH.axis, axisLabel: { color: '#8b949e', fontSize: 9, hideOverlap: true, formatter: '{HH}:{mm}:{ss}' } },
    yAxis: { type: 'value', name: 'ms', nameTextStyle: { color: '#58a6ff', fontSize: 10 }, ...CH.axis },
    series: [
      { name: '平均延迟', type: 'line', showSymbol: false, smooth: true, data: d.map((p, i) => [x[i], p.avg]),
        lineStyle: { width: 2, color: '#58a6ff' } },
      { name: 'P99 延迟', type: 'line', showSymbol: false, smooth: true, data: d.map((p, i) => [x[i], p.p99]),
        lineStyle: { width: 2, color: '#f85149' }, areaStyle: { color: areaGrad('#f85149') } },
    ],
  });
  setOpt('chart-lt-err', {
    ...CH, legend: { show: false },
    xAxis: { type: 'time', ...CH.axis, axisLabel: { color: '#8b949e', fontSize: 9, hideOverlap: true, formatter: '{HH}:{mm}:{ss}' } },
    yAxis: { type: 'value', name: '%', max: 100, nameTextStyle: { color: '#f85149', fontSize: 10 }, ...CH.axis },
    series: [{ name: '错误率', type: 'line', showSymbol: false, smooth: true, data: d.map((p, i) => [x[i], p.err]),
      lineStyle: { width: 2, color: '#f85149' }, areaStyle: { color: areaGrad('#f85149') },
      markLine: { silent: true, symbol: 'none', data: [{ yAxis: 20, lineStyle: { color: '#d29922', type: 'dashed' },
        label: { formatter: '熔断阈值 20%', color: '#d29922', fontSize: 10 } }] } }],
  });
  setOpt('chart-lt-gpu', {
    ...CH, legend: { ...CH.legend, data: ['GPU 利用率', '显存 GiB'] },
    xAxis: { type: 'time', ...CH.axis, axisLabel: { color: '#8b949e', fontSize: 9, hideOverlap: true, formatter: '{HH}:{mm}:{ss}' } },
    yAxis: [
      { type: 'value', name: '%', max: 100, nameTextStyle: { color: '#3fb950', fontSize: 10 }, ...CH.axis },
      { type: 'value', name: 'GiB', nameTextStyle: { color: '#bc8cff', fontSize: 10 }, ...CH.axis, splitLine: { show: false } },
    ],
    series: [
      { name: 'GPU 利用率', type: 'line', showSymbol: false, smooth: true, data: d.map((p, i) => [x[i], p.gpu]),
        lineStyle: { width: 2, color: '#3fb950' }, areaStyle: { color: areaGrad('#3fb950') } },
      { name: '显存 GiB', type: 'line', yAxisIndex: 1, showSymbol: false, smooth: true, data: d.map((p, i) => [x[i], p.vram]),
        lineStyle: { width: 2, color: '#bc8cff' } },
    ],
  });
}
function renderLtStages(stages) {
  S.lt.stages = stages || [];
  $('ltStageTable').innerHTML = S.lt.stages.length ? S.lt.stages.map((s) => `
    <tr><td class="num"><b>${s.concurrency}</b></td>
      <td class="num">${s.planned_seconds}s</td>
      <td class="num">${s.total}</td>
      <td class="num">${s.ok}</td>
      <td class="num"><b style="color:var(--green)">${fmt(s.qps, 2)}</b></td>
      <td class="num">${fmt(s.avg_tps, 2)}</td>
      <td class="num">${fmt(s.avg_ttft_ms, 1)}</td>
      <td class="num">${fmt(s.avg_e2e_ms, 0)}</td>
      <td class="num">${fmt(s.p99_e2e_ms, 0)}</td>
      <td class="num" style="color:${(s.error_rate || 0) > 0.05 ? 'var(--red)' : 'inherit'}">${fmt((s.error_rate || 0) * 100, 2)}%</td>
    </tr>`).join('') : '<tr><td colspan="10" class="empty">尚无阶梯数据</td></tr>';
}
function renderLtReport(summary, maxQps, stopReason) {
  const best = S.lt.stages.filter((s) => s.qps).sort((a, b) => b.qps - a.qps)[0];
  $('ltReport').innerHTML = `
    ${stopReason ? `<div class="card mb12" style="border-color:var(--red)"><b style="color:var(--red)">熔断停止</b><div class="small mt8">${esc(stopReason)}</div></div>` : ''}
    <div class="metric-list">
      ${[
        ['最大 QPS', maxQps !== null && maxQps !== undefined ? `${fmt(maxQps, 2)}` : '—'],
        ['对应并发数', best ? `${best.concurrency}` : '—'],
        ['峰值 P99 延迟', `${fmt(summary ? summary.p99_e2e_ms : null, 0)} ms`],
        ['平均延迟', `${fmt(summary ? summary.avg_e2e_ms : null, 0)} ms`],
        ['平均 TTFT', `${fmt(summary ? summary.avg_ttft_ms : null, 1)} ms`],
        ['平均生成速度', `${fmt(summary ? summary.avg_tps : null, 2)} tok/s`],
        ['请求总数', summary ? `${summary.total}（成功 ${summary.ok} / 失败 ${summary.errors}）` : '—'],
        ['错误率', summary ? `${fmt(summary.error_rate * 100, 2)}%` : '—'],
        ['生成 Token 总量', summary ? fmtInt(summary.total_tokens) : '—'],
      ].map(([k, v]) => `<div class="metric-item"><span class="k">${k}</span><span class="v" style="font-size:13px">${v}</span></div>`).join('')}
    </div>`;
}
$('btnLtReport').onclick = async () => {
  if (!S.lt.runId) return toast('还没有压测记录', 'warn');
  try {
    const r = await api('GET', `/api/runs/${S.lt.runId}/report`);
    openModal(`压测报告 · Run #${S.lt.runId}`, `<pre>${esc(r.markdown)}</pre>`, r.markdown);
  } catch (e) { toast(e.message, 'err'); }
};

/* ------------------------------ 6. 历史对比 ------------------------------ */
async function queryRuns() {
  const q = new URLSearchParams();
  if ($('fModel').value.trim()) q.set('model', $('fModel').value.trim());
  if ($('fType').value) q.set('type', $('fType').value);
  if ($('fFrom').value) q.set('date_from', $('fFrom').value);
  if ($('fTo').value) q.set('date_to', $('fTo').value);
  q.set('limit', '200');
  try {
    const r = await api('GET', `/api/runs?${q.toString()}`);
    S.hist.runs = r.runs;
    S.hist.selected.clear();
    renderHistTable();
    $('histCount').textContent = `共 ${r.count} 条记录`;
  } catch (e) { toast(e.message, 'err'); }
}
function renderHistTable() {
  const tb = $('histTable');
  if (!S.hist.runs.length) { tb.innerHTML = '<tr><td colspan="15" class="empty">没有符合条件的记录</td></tr>'; return; }
  const typeBadge = { single: 'blue', benchmark: 'purple', loadtest: 'yellow', eval: 'green' };
  const typeName = { single: '单条', benchmark: '基准', loadtest: '压测', eval: '评测' };
  const stBadge = { done: 'green', running: 'blue', error: 'red', stopped: 'yellow', interrupted: 'yellow' };
  tb.innerHTML = S.hist.runs.map((r) => `
    <tr>
      <td><input type="checkbox" data-id="${r.id}" ${S.hist.selected.has(r.id) ? 'checked' : ''}></td>
      <td class="mono">${r.id}</td>
      <td class="small muted nowrap">${esc(r.started_at || '')}</td>
      <td><span class="badge ${typeBadge[r.run_type] || ''}">${typeName[r.run_type] || r.run_type}</span></td>
      <td><strong>${esc(r.model)}</strong></td>
      <td class="small">${esc(r.conn_name || '—')}</td>
      <td class="num">${r.concurrency || 1}</td>
      <td class="num">${fmtInt(r.total_reqs)}</td>
      <td class="num"><b>${fmt(r.avg_tps, 2)}</b></td>
      <td class="num">${fmt(r.avg_ttft_ms, 1)}</td>
      <td class="num">${fmt(r.p50_e2e_ms, 0)}</td>
      <td class="num">${fmt(r.p99_e2e_ms, 0)}</td>
      <td class="num" style="color:${(r.error_rate || 0) > 0.05 ? 'var(--red)' : 'inherit'}">${fmt((r.error_rate || 0) * 100, 2)}%</td>
      <td><span class="badge ${stBadge[r.status] || ''}">${esc(r.status)}</span></td>
      <td class="nowrap">
        <button class="btn sm" data-act="detail" data-id="${r.id}">详情</button>
        <button class="btn sm" data-act="report" data-id="${r.id}">报告</button>
        <button class="btn sm" data-act="csv" data-id="${r.id}">CSV</button>
        <button class="btn sm" data-act="json" data-id="${r.id}">JSON</button>
        <button class="btn sm danger" data-act="del" data-id="${r.id}">删除</button>
      </td>
    </tr>`).join('');
}
$('histTable').onchange = (e) => {
  const cb = e.target.closest('input[type=checkbox][data-id]');
  if (!cb) return;
  const id = Number(cb.dataset.id);
  if (cb.checked) { if (S.hist.selected.size >= 6) { cb.checked = false; return toast('最多同时对比 6 条', 'warn'); } S.hist.selected.add(id); }
  else S.hist.selected.delete(id);
};
$('histTable').onclick = async (e) => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  const id = Number(btn.dataset.id);
  const act = btn.dataset.act;
  try {
    if (act === 'detail') await showRunDetail(id);
    else if (act === 'report') {
      const r = await api('GET', `/api/runs/${id}/report`);
      openModal(`测试报告 · Run #${id}`, `<pre>${esc(r.markdown)}</pre>`, r.markdown);
    } else if (act === 'csv') window.open(`/api/runs/${id}/export?fmt=csv`, '_blank');
    else if (act === 'json') window.open(`/api/runs/${id}/export?fmt=json`, '_blank');
    else if (act === 'del') {
      if (!confirm(`确认删除测试记录 #${id}？关联的请求明细会一并删除。`)) return;
      await api('DELETE', `/api/runs/${id}`);
      toast('已删除', 'ok'); queryRuns();
    }
  } catch (err) { toast(err.message, 'err'); }
};
async function showRunDetail(id) {
  const d = await api('GET', `/api/runs/${id}`);
  const r = d.run, st = d.stats;
  const env = r.env || {};
  const ev = (r.summary && r.summary.eval) || null;
  const rows = [
    ['类型 / 状态', `${r.run_type} / ${r.status}`],
    ['模型', r.model], ['连接档案', r.conn_name || '—'],
    ['开始', r.started_at || '—'], ['结束', r.finished_at || '—'],
    ['并发数', r.concurrency || 1], ['总耗时', `${fmt((r.duration_ms || 0) / 1000, 1)} s`],
  ];
  if (ev) {
    const P = (v) => (v === null || v === undefined) ? '—' : (v * 100).toFixed(1) + '%';
    rows.push(['评测套件', `${ev.suite_name || ev.suite_id}（${ev.mode}）`]);
    rows.push(['题量 / 判分', `${ev.total || 0} / ${ev.graded || 0}`]);
    if (ev.accuracy !== null && ev.accuracy !== undefined) rows.push(['准确率', P(ev.accuracy)]);
    if (ev.pass_at_1 !== null && ev.pass_at_1 !== undefined) rows.push(['pass@1 / pass@k', `${P(ev.pass_at_1)} / ${P(ev.pass_at_k)}`]);
    if (ev.rougeL !== null && ev.rougeL !== undefined) rows.push(['ROUGE-L / BLEU / CHRF++', `${fmt(ev.rougeL, 3)} / ${fmt(ev.bleu, 3)} / ${fmt(ev.chrf, 3)}`]);
    if (ev.f1 !== null && ev.f1 !== undefined) rows.push(['F1 / P / R', `${fmt(ev.f1, 3)} / ${fmt(ev.precision, 3)} / ${fmt(ev.recall, 3)}`]);
    if (ev.instruction_rate !== null && ev.instruction_rate !== undefined) rows.push(['指令遵循率', P(ev.instruction_rate)]);
    if (ev.refusal_rate !== null && ev.refusal_rate !== undefined) rows.push(['有害拒绝率 / 泄露率', `${P(ev.refusal_rate)} / ${P(ev.harmful_leak_rate)}`]);
    if (ev.jailbreak_rate !== null && ev.jailbreak_rate !== undefined) rows.push(['越狱成功率', P(ev.jailbreak_rate)]);
    if (ev.hallucination_rate !== null && ev.hallucination_rate !== undefined) rows.push(['幻觉率', P(ev.hallucination_rate)]);
    if (ev.score !== null && ev.score !== undefined) rows.push(['综合得分', `${fmt(ev.score, 2)}（${ev.score_source || ''}）`]);
  } else {
    rows.push(
      ['请求总数', `${st.total}（成功 ${st.ok} / 失败 ${st.errors}）`],
      ['错误率', `${fmt((st.error_rate || 0) * 100, 2)}%`],
      ['平均 / 最大 TPS', `${fmt(st.avg_tps, 2)} / ${fmt(st.max_tps, 2)} tok/s`],
    );
  }
  rows.push(
    ['Prefill / Decode 吞吐', `${fmt(st.prefill_tps, 1)} / ${fmt(st.decode_tps, 1)} tok/s（${st.prefill_source || '不可测'}）`],
    ['整体吞吐', `${fmt(st.throughput_tps, 1)} tok/s`],
    ['平均 TTFT', `${fmt(st.avg_ttft_ms, 1)} ms`],
    ['TTFT P50/P90/P95/P99', `${fmt(st.p50_ttft_ms, 0)} / ${fmt(st.p90_ttft_ms, 0)} / ${fmt(st.p95_ttft_ms, 0)} / ${fmt(st.p99_ttft_ms, 0)} ms`],
    ['E2E P50/P90/P95/P99', `${fmt(st.p50_e2e_ms, 0)} / ${fmt(st.p90_e2e_ms, 0)} / ${fmt(st.p95_e2e_ms, 0)} / ${fmt(st.p99_e2e_ms, 0)} ms`],
    ['GPU 峰值利用率', `${fmt(r.gpu_peak_util, 1)}%`],
    ['VRAM 峰值', `${fmt(r.gpu_peak_vram, 0)} MB`],
    ['环境 GPU', (env.gpu_names || []).join(', ') || '—'],
    ['环境内存', env.ram_total_mb ? `${fmt(env.ram_total_mb / 1024, 1)} GiB` : '—'],
  );
  const rec = (r.summary && r.summary.recommend) || null;
  $('histDetail').innerHTML = `
    <div class="metric-list">
      ${rows.map(([k, v]) => `<div class="metric-item"><span class="k">${esc(k)}</span><span class="v" style="font-size:12px">${esc(v)}</span></div>`).join('')}
    </div>
    ${rec && rec.ok ? `<div class="small mt12" style="color:var(--accent)">建议并发上限：<b>${esc(rec.recommended)}</b>（${esc(rec.reason)}）</div>` : ''}
    <div class="small muted mt12">${ev
      ? `评测逐题明细请点「报告」查看，或到评测中心重新载入该 Run。`
      : `请求明细 ${d.details.length} 条${d.details.length > 50 ? '（仅展示前 50 条）' : ''}`}</div>`;
}
$('btnQueryRuns').onclick = queryRuns;
$('btnResetFilter').onclick = () => {
  ['fModel', 'fFrom', 'fTo'].forEach((k) => { $(k).value = ''; });
  $('fType').value = ''; queryRuns();
};
$('btnCompareRuns').onclick = async () => {
  if (S.hist.selected.size < 1) return toast('请先勾选至少一条记录', 'warn');
  const ids = [...S.hist.selected].join(',');
  try {
    const r = await api('GET', `/api/runs/compare?ids=${ids}`);
    const items = r.items;
    const labels = items.map((i) => `#${i.run.id} ${i.run.model}${i.run.concurrency > 1 ? ` (${i.run.concurrency}c)` : ''}`);
    setOpt('chart-hist', {
      ...CH, legend: { ...CH.legend, data: ['平均 TPS', '平均 TTFT', '平均 E2E', 'P99 E2E'] },
      grid: { left: 50, right: 56, top: 34, bottom: 70 },
      xAxis: { type: 'category', data: labels, ...CH.axis, axisLabel: { color: '#8b949e', fontSize: 10, rotate: 20, interval: 0 } },
      yAxis: [
        { type: 'value', name: 'TPS', nameTextStyle: { color: '#3fb950', fontSize: 10 }, ...CH.axis },
        { type: 'value', name: 'ms', nameTextStyle: { color: '#58a6ff', fontSize: 10 }, ...CH.axis, splitLine: { show: false } },
      ],
      series: [
        { name: '平均 TPS', type: 'bar', data: items.map((i) => i.stats.avg_tps),
          itemStyle: { color: '#238636', borderRadius: [3, 3, 0, 0] }, barMaxWidth: 30 },
        { name: '平均 TTFT', type: 'line', yAxisIndex: 1, data: items.map((i) => i.stats.avg_ttft_ms),
          lineStyle: { color: '#58a6ff', width: 2 }, itemStyle: { color: '#58a6ff' } },
        { name: '平均 E2E', type: 'line', yAxisIndex: 1, data: items.map((i) => i.stats.avg_e2e_ms),
          lineStyle: { color: '#d29922', width: 2, type: 'dashed' }, itemStyle: { color: '#d29922' } },
        { name: 'P99 E2E', type: 'line', yAxisIndex: 1, data: items.map((i) => i.stats.p99_e2e_ms),
          lineStyle: { color: '#f85149', width: 2 }, itemStyle: { color: '#f85149' } },
      ],
    }, false);
    toast(`已对比 ${items.length} 条记录`, 'ok');
  } catch (e) { toast(e.message, 'err'); }
};

/* ------------------------------ 7. 告警 ------------------------------ */
const RULE_META = {
  tps_low: { name: '生成速度 TPS 过低', unit: 'tok/s', step: 1 },
  ttft_high: { name: '首 Token 延迟过高', unit: 'ms', step: 100 },
  e2e_high: { name: '端到端延迟过高', unit: 'ms', step: 1000 },
  vram_high: { name: '显存占用过高', unit: '%', step: 1 },
  gpu_temp_high: { name: 'GPU 温度过高', unit: '°C', step: 1 },
  error_rate: { name: '请求错误率过高', unit: '（0~1）', step: 0.05 },
  gpu_idle: { name: 'GPU 长时间空闲', unit: '%', step: 1 },
};
function renderRules(rules) {
  S.alerts.rules = rules;
  $('alertsEnabled').checked = !!rules.enabled;
  $('rulesList').innerHTML = Object.entries(rules.rules).map(([key, r]) => {
    const meta = RULE_META[key] || { name: key, unit: '', step: 1 };
    return `<div class="metric-item" style="gap:8px;align-items:center">
      <label style="display:flex;gap:8px;align-items:center;flex:1;cursor:pointer">
        <input type="checkbox" data-rule="${key}" ${r.enabled ? 'checked' : ''}>
        <span style="margin:0;font-size:12.5px;color:var(--fg)">${meta.name}</span>
      </label>
      <input type="number" data-ruleval="${key}" value="${r.value}" step="${meta.step}"
        style="width:104px;padding:5px 8px;font-size:12px">
      <span class="small muted" style="width:52px;text-align:right">${meta.unit}</span>
    </div>`;
  }).join('');
}
async function loadAlerts() {
  try {
    const r = await api('GET', '/api/alerts?limit=200');
    S.alerts.list = r.alerts;
    renderRules(r.rules);
    renderAlertTable();
  } catch (e) { toast(e.message, 'err'); }
}
async function refreshAlerts() {
  try {
    const r = await api('GET', '/api/alerts?limit=200');
    const prev = S.alerts.list.length;
    S.alerts.list = r.alerts;
    renderAlertTable();
    const fresh = r.alerts.filter((a) => !a.acked);
    if (S.alerts.notify && fresh.length && r.alerts.length > prev) {
      const a = r.alerts[0];
      try {
        new Notification('LLM Monitor 告警', { body: `${a.message}`, tag: a.rule_key });
      } catch (e) {}
    }
  } catch (e) {}
}
function renderAlertTable() {
  const tb = $('alertTable');
  if (!S.alerts.list.length) { tb.innerHTML = '<tr><td colspan="6" class="empty">暂无告警</td></tr>'; return; }
  tb.innerHTML = S.alerts.list.map((a) => `
    <tr style="${a.acked ? 'opacity:.55' : ''}">
      <td class="small muted nowrap">${hhmmss(a.ts)}</td>
      <td><span class="badge ${a.level === 'critical' ? 'red' : a.level === 'warning' ? 'yellow' : 'blue'}">${esc(a.level)}</span></td>
      <td>${esc(a.metric)}</td>
      <td class="num">${fmt(a.value, 2)}</td>
      <td class="num">${fmt(a.threshold, 2)}</td>
      <td class="small">${esc(a.message)}</td>
    </tr>`).join('');
}
$('btnSaveRules').onclick = async () => {
  const rules = { enabled: $('alertsEnabled').checked, rules: {} };
  document.querySelectorAll('#rulesList input[data-ruleval]').forEach((el) => {
    const key = el.dataset.ruleval;
    const cb = document.querySelector(`#rulesList input[data-rule="${key}"]`);
    rules.rules[key] = { enabled: cb ? cb.checked : false, value: Number(el.value) };
  });
  try {
    const r = await api('PUT', '/api/alerts/rules', rules);
    renderRules(r.rules);
    toast('告警规则已保存', 'ok');
  } catch (e) { toast(e.message, 'err'); }
};
$('btnRefreshAlerts').onclick = () => { loadAlerts(); toast('已刷新', 'ok', 1500); };
$('btnAckAll').onclick = async () => {
  await api('POST', '/api/alerts/ack', null);
  toast('已全部标记已读', 'ok'); loadAlerts();
};
$('btnClearAlerts').onclick = async () => {
  if (!confirm('确认清空全部告警日志？')) return;
  await api('DELETE', '/api/alerts');
  toast('已清空', 'ok'); loadAlerts();
};
$('btnRequestNotify').onclick = async () => {
  if (!('Notification' in window)) return toast('当前浏览器不支持桌面通知', 'warn');
  const p = await Notification.requestPermission();
  S.alerts.notify = p === 'granted';
  $('notifyEnabled').checked = S.alerts.notify;
  toast(S.alerts.notify ? '桌面通知已启用' : '通知权限被拒绝', S.alerts.notify ? 'ok' : 'warn');
};
$('notifyEnabled').onchange = () => {
  S.alerts.notify = $('notifyEnabled').checked
    && typeof Notification !== 'undefined' && Notification.permission === 'granted';
  if ($('notifyEnabled').checked && !S.alerts.notify) $('btnRequestNotify').click();
};

/* ------------------------------ 进度 / 完成事件 ------------------------------ */
function handleProgress(p) {
  if (p.run_type === 'benchmark') {
    benchProgress(p);
    if (p.stop_reason) { toast(`套件熔断/停止：${p.stop_reason}`, 'warn'); }
  } else if (p.run_type === 'loadtest') {
    ltPush(p);
  }
}
function handleStageDone(msg) {
  const idx = msg.stage_index;
  const list = S.lt.stages.slice();
  list[idx] = msg.stage;
  renderLtStages(list);
  logLine('LOAD', `阶梯 ${msg.stage.concurrency} 并发完成：QPS ${fmt(msg.stage.qps, 2)}，错误率 ${fmt(msg.stage.error_rate * 100, 2)}%`, 'info');
}
async function handleRunDone(msg) {
  if (msg.run_type === 'benchmark') {
    $('btnRunBench').disabled = false; $('btnStopBench').disabled = true;
    $('benchProgress').innerHTML = `<span class="badge ${msg.status === 'done' ? 'green' : 'yellow'}">套件${msg.status === 'done' ? '完成' : '已停止'}</span>`;
    try {
      const d = await api('GET', `/api/runs/${msg.run_id}`);
      const cases = (msg.cases && msg.cases.length) ? msg.cases : (d.run.summary && d.run.summary.cases) || [];
      renderBenchResults(cases, d.stats, d.boxplot);
      logLine('BENCH', `套件完成：平均 TPS ${fmt(d.stats.avg_tps, 2)}，P99 E2E ${fmt(d.stats.p99_e2e_ms, 0)} ms`, 'ok');
      toast(`基准套件完成，日志已落库 Run #${msg.run_id}`, 'ok');
    } catch (e) { toast(e.message, 'err'); }
  } else if (msg.run_type === 'loadtest') {
    S.lt.running = false; toggleLtButtons(false);
    if (msg.stages) renderLtStages(msg.stages);
    renderLtReport(msg.summary, msg.max_qps, msg.stop_reason);
    if (msg.stop_reason) {
      toast(`压测被熔断：${esc(msg.stop_reason)}`, 'warn', 7000);
      logLine('LOAD', `熔断停止：${msg.stop_reason}`, 'warn');
    } else {
      toast(`压测完成，最大 QPS ${fmt(msg.max_qps, 2)}`, 'ok');
      logLine('LOAD', `压测完成，最大 QPS ${fmt(msg.max_qps, 2)}`, 'ok');
    }
  } else if (msg.status === 'error' && msg.error) {
    toast(`任务失败：${esc(msg.error)}`, 'err');
  }
}

/* ------------------------------ 路由与初始化 ------------------------------ */
$('tabs').onclick = (e) => {
  const btn = e.target.closest('button[data-panel]');
  if (!btn) return;
  switchPanel(btn.dataset.panel);
};
function switchPanel(panel) {
  S.panel = panel;
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('active', b.dataset.panel === panel));
  document.querySelectorAll('.panel').forEach((p) => p.classList.toggle('active', p.id === `panel-${panel}`));
  if (panel === 'dashboard') {
    renderDashCharts(); refreshNativeMetrics();
    // 对话测试已并入实时看板，进入时补拉模型列表并同步会话面板
    if ($('chatModel') && !$('chatModel').options.length) loadModelsInto('chatModel', $('chatConn').value);
    if (window.CHAT) CHAT.onPanel();
  }
  if (panel === 'history') queryRuns();
  if (panel === 'alerts') loadAlerts();
  if (panel === 'benchmark') loadModelsInto('benchModel', $('benchConn').value);
  if (panel === 'loadtest') { loadModelsInto('ltModel', $('ltConn').value); updateLtPlan(); }
  if (panel === 'eval' && window.EVAL) EVAL.onPanel();
  resizePanelCharts(panel);
}
$('chatConn').onchange = () => loadModelsInto('chatModel', $('chatConn').value);
$('benchConn').onchange = () => loadModelsInto('benchModel', $('benchConn').value);
$('ltConn').onchange = () => loadModelsInto('ltModel', $('ltConn').value);

document.querySelectorAll('.win-btn').forEach((b) => {
  b.onclick = () => {
    S.win = Number(b.dataset.win);
    document.querySelectorAll('.win-btn').forEach((x) => x.classList.toggle('active', x === b));
    renderDashCharts();
  };
});
/* 采样状态徽标。原来这句直接写 $('intervalTxt')，但那个 span 位于被注释掉的
   sec-desc 里（双栏改版后不再渲染），于是每条 hello 消息都会抛
   "Cannot set properties of null"。改为统一从这个函数出，顺带把采样间隔显示出来。 */
function paintSampleBadge() {
  const el = $('sampleBadge');
  if (!el) return;
  el.textContent = S.paused ? '已暂停' : `采样中 · ${S.sampleInterval}s`;
  el.style.color = S.paused ? 'var(--yellow)' : '';
}
$('btnPauseDash').onclick = () => {
  S.paused = !S.paused;
  $('btnPauseDash').textContent = S.paused ? '恢复刷新' : '暂停刷新';
  paintSampleBadge();
};
$('btnClearLog').onclick = () => { $('dashLog').innerHTML = ''; };
window.addEventListener('beforeunload', () => { try { ws && ws.close(); } catch (e) {} });

async function boot() {
  setWsStatus('connecting', '初始化…');
  logLine('SYS', '正在初始化控制台…', 'info');
  try {
    await loadPresets();
    await refreshConnections();
    await loadSuites();
    await loadSystemInfo();
    connectWs();
    // 首屏补齐历史曲线
    const hist = await api('GET', '/api/metrics/realtime?seconds=300');
    (hist.samples || []).forEach((s) => S.samples.push(s));
    trimSamples();
    if (S.samples.length) { updateKpis(S.samples[S.samples.length - 1]); renderDashCharts(); }
    refreshNativeMetrics();
    loadAlerts();
    setInterval(refreshNativeMetrics, 15000);
    setInterval(loadSystemInfo, 30000);
    logLine('SYS', `初始化完成：${S.connections.length} 个连接档案，采样间隔 ${S.sampleInterval}s`, 'ok');
    if (!S.connections.length) toast('还没有连接档案，请到「连接管理」新增或使用内置演示后端', 'warn', 7000);
  } catch (e) {
    logLine('SYS', `初始化失败: ${e.message}`, 'err');
    toast(`初始化失败：${e.message}`, 'err');
  }
}
boot();
