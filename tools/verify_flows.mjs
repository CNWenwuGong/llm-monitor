/**
 * 交互式流程验证：逐个切换面板 + 真实跑一次压测 + 真实发一条对话，
 * 全程收集 console 错误，并分批截图。
 */
const CDP_HTTP = process.env.CDP_HTTP || 'http://127.0.0.1:9222';
const BASE = process.env.TARGET_URL || 'http://127.0.0.1:8080';
const OUTDIR = process.env.OUTDIR || '.';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pickTarget() {
  const list = await (await fetch(`${CDP_HTTP}/json/list`)).json();
  const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
  if (!page) throw new Error('没有可用的 page target');
  return page;
}
function makeClient(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  const listeners = [];
  const ready = new Promise((res, rej) => { ws.onopen = () => res(); ws.onerror = () => rej(new Error('ws fail')); });
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id);
      m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result);
    } else if (m.method) listeners.forEach((f) => f(m));
  };
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const mid = ++id; pending.set(mid, { resolve, reject });
    ws.send(JSON.stringify({ id: mid, method, params }));
    setTimeout(() => { if (pending.has(mid)) { pending.delete(mid); reject(new Error(method + ' timeout')); } }, 40000);
  });
  return { ready, send, on: (f) => listeners.push(f), close: () => ws.close() };
}

(async () => {
  const fs = await import('node:fs');
  const target = await pickTarget();
  const c = makeClient(target.webSocketDebuggerUrl);
  await c.ready;
  await c.send('Runtime.enable');
  await c.send('Log.enable');
  await c.send('Page.enable');

  const errors = [], cErrs = [];
  c.on((m) => {
    if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error')
      cErrs.push((m.params.args || []).map((a) => a.value ?? a.description).join(' '));
    if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') cErrs.push(m.params.entry.text);
  });

  await c.send('Page.navigate', { url: BASE });
  await sleep(6000);

  const evalJs = async (expr) => (await c.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })).result.value;
  const shot = async (name) => {
    const s = await c.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    fs.writeFileSync(`${OUTDIR}/${name}.png`, Buffer.from(s.data, 'base64'));
  };
  const goto = async (p) => { await evalJs(`document.querySelector('#tabs button[data-panel="${p}"]').click()`); await sleep(1600); };
  const report = {};

  /* ---- 面板截图巡检 ---- */
  for (const p of ['connections', 'chat', 'benchmark', 'loadtest', 'history', 'alerts']) {
    await goto(p);
    report[p] = await evalJs(`({ active: !!document.querySelector('#panel-${p}.active'),
      charts: Array.from(document.querySelectorAll('#panel-${p} .chart')).filter(el => window.echarts && echarts.getInstanceByDom(el)).length })`);
    await shot('panel-' + p);
  }

  /* ---- 压测流程 ---- */
  await goto('loadtest');
  await evalJs(`(() => {
    const set = (id, v) => { const el = document.getElementById(id); el.value = v; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); };
    set('ltMode', 'stage'); document.getElementById('ltMode').dispatchEvent(new Event('change', {bubbles:true}));
    set('ltStartC', '1'); set('ltMaxC', '4'); set('ltStageSec', '6'); set('ltDuration', '0');
    set('ltMaxTokens', '96');
    return document.getElementById('ltPlan').textContent;
  })()`);
  report.stagePlanText = await evalJs(`document.getElementById('ltPlan').textContent`);
  await evalJs(`document.getElementById('btnStartLt').click()`);
  await sleep(4000);
  report.ltRunning = await evalJs(`document.getElementById('btnStartLt').disabled`);
  await shot('flow-loadtest-running');
  // 等压测跑完（1→2→4 各 6 秒 + 收尾）
  for (let i = 0; i < 12; i++) {
    await sleep(4000);
    const done = await evalJs(`!document.getElementById('btnStartLt').disabled`);
    if (done) break;
  }
  report.ltSummary = await evalJs(`({
    qps: document.getElementById('lt-qps').textContent,
    avg: document.getElementById('lt-avg').textContent,
    p99: document.getElementById('lt-p99').textContent,
    err: document.getElementById('lt-err').textContent,
    stageRows: document.querySelectorAll('#ltStageTable tr').length,
    stageText: document.getElementById('ltStageTable').innerText.slice(0, 400),
    reportItems: document.querySelectorAll('#ltReport .metric-item').length,
    reportText: document.getElementById('ltReport').innerText.slice(0, 300),
    liveSeries: (window.__ltSeriesLen || 'n/a'),
  })`);
  await shot('flow-loadtest-done');

  /* ---- 单条对话流程 ---- */
  await goto('chat');
  await evalJs(`(() => {
    const t = document.getElementById('chatInput');
    t.value = '用一句话解释什么是 KV Cache。';
    t.dispatchEvent(new Event('input', {bubbles:true}));
    // 「不限制回答长度」现在是默认勾选的，要设一个具体的 max_tokens 必须先把它关掉，
    // 否则面板不会把该字段下发给后端（输入框在勾选状态下是 disabled 的）。
    const un = document.getElementById('chatUnlimited');
    if (un && un.checked) { un.checked = false; un.dispatchEvent(new Event('change', {bubbles:true})); }
    const mt = document.getElementById('chatMaxTokens');
    mt.value = '120';
    mt.dispatchEvent(new Event('input', {bubbles:true}));
    document.getElementById('btnSend').click();
  })()`);
  for (let i = 0; i < 15; i++) {
    await sleep(2000);
    const st = await evalJs(`document.getElementById('chatStatus').textContent`);
    if (st && /完成|失败|停止|结束/.test(st)) break;
  }
  report.chat = await evalJs(`({
    status: document.getElementById('chatStatus').textContent,
    ttft: document.getElementById('m-ttft').textContent,
    e2e: document.getElementById('m-e2e').textContent,
    tps: document.getElementById('m-tps').textContent,
    ct: document.getElementById('m-ct').textContent,
    msgs: document.querySelectorAll('#chatBox .msg').length,
    answerPreview: (document.querySelector('#chatBox .msg.ai .text') || {}).innerText?.slice(0, 160) || '',
  })`);
  await shot('flow-chat-done');

  /* ---- 基准套件（冒烟） ---- */
  await goto('benchmark');
  await evalJs(`(() => {
    const s = document.getElementById('benchSuite'); s.value = 'smoke';
    s.dispatchEvent(new Event('change', {bubbles:true}));
    document.getElementById('benchMaxTokens').value = '160';
    document.getElementById('btnRunBench').click();
  })()`);
  for (let i = 0; i < 25; i++) {
    await sleep(3000);
    const done = await evalJs(`!document.getElementById('btnRunBench').disabled`);
    if (done) break;
  }
  report.benchmark = await evalJs(`({
    caseRows: document.querySelectorAll('#benchCaseTable tr').length,
    summaryItems: document.querySelectorAll('#benchSummary .metric-item').length,
    boxChart: !!(window.echarts && echarts.getInstanceByDom(document.getElementById('chart-bench-box'))),
    barChart: !!(window.echarts && echarts.getInstanceByDom(document.getElementById('chart-bench-bar'))),
    text: document.getElementById('benchSummary').innerText.slice(0, 260),
  })`);
  await shot('flow-benchmark-done');

  /* ---- 历史面板 ---- */
  await goto('history');
  await sleep(1200);
  report.history = await evalJs(`({
    rows: document.querySelectorAll('#histTable tr').length,
    count: document.getElementById('histCount').textContent,
    text: document.getElementById('histTable').innerText.slice(0, 420),
  })`);
  // 勾选前两条做对比
  await evalJs(`(() => {
    const cbs = document.querySelectorAll('#histTable input[type=checkbox]');
    for (let i = 0; i < Math.min(2, cbs.length); i++) { cbs[i].checked = true; cbs[i].dispatchEvent(new Event('change', {bubbles:true})); }
    document.getElementById('btnCompareRuns').click();
  })()`);
  await sleep(2200);
  report.compare = await evalJs(`({ chart: !!(window.echarts && echarts.getInstanceByDom(document.getElementById('chart-hist'))) })`);
  await shot('flow-history-compare');

  /* ---- 告警面板 ---- */
  await goto('alerts');
  report.alerts = await evalJs(`({
    ruleRows: document.querySelectorAll('#rulesList .metric-item').length,
    alertRows: document.querySelectorAll('#alertTable tr').length,
    text: document.getElementById('alertTable').innerText.slice(0, 300),
  })`);
  await shot('flow-alerts');

  report.uncaughtExceptions = errors;
  report.consoleErrors = cErrs;
  console.log(JSON.stringify(report, null, 2));
  c.close();
  process.exit(0);
})().catch((e) => { console.error('FLOW_FAILED', e.message); process.exit(1); });
