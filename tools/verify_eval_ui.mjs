/**
 * 评测中心面板的无头验证：所有 tab 巡检 + 评测真实跑一遍 + 指标清单/效率指标渲染断言。
 *
 *   TARGET_URL=http://127.0.0.1:8099 OUTDIR=shots node tools/verify_eval_ui.mjs
 */
const CDP_HTTP = process.env.CDP_HTTP || 'http://127.0.0.1:9222';
const BASE = process.env.TARGET_URL || 'http://127.0.0.1:8080';
const OUTDIR = process.env.OUTDIR || '.';
const SUITE = process.env.EVAL_SUITE || 'mmlu_mini';
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
    setTimeout(() => { if (pending.has(mid)) { pending.delete(mid); reject(new Error(method + ' timeout')); } }, 90000);
  });
  return { ready, send, on: (f) => listeners.push(f), close: () => ws.close() };
}

(async () => {
  const fs = await import('node:fs');

  /* ---------- 0. 前置闸门：服务端返回的 JS 必须能解析 ----------
     踩过一次坑：模板串里嵌三元时漏了一个 else 分支，整个 eval.js 静默失效，
     页面只是"看起来没加载完"，控制台仅一行 SyntaxError，很容易误判成网络慢。
     这里在连浏览器之前先把脚本抓下来解析一遍，让这类错误最早暴露。 */
  const syntaxFailures = [];
  for (const name of ['/app.js', '/eval.js']) {
    try {
      const code = await (await fetch(BASE + name)).text();
      new Function(code);            // 仅解析，不执行
    } catch (e) {
      syntaxFailures.push(`${name}: ${e.message}`);
    }
  }
  if (syntaxFailures.length) {
    console.error('FRONTEND_SYNTAX_FAILED:\n  - ' + syntaxFailures.join('\n  - '));
    process.exit(3);
  }
  // 走 stderr：stdout 要保持是纯 JSON，方便管道给 jq / python 解析
  process.stderr.write('前端脚本语法检查通过（/app.js, /eval.js）\n');

  /* DOM 契约闸门：JS 里 $('x') 引用的 id 必须真的存在。
     改版时删掉一段 HTML 却忘了 JS 还在写它，不会有语法错，只在运行时抛
     "Cannot set properties of null"，控制台一闪而过。 */
  const stripHtmlComments = (s) => s.replace(/<!--[\s\S]*?-->/g, ' ');
  const stripJsComments = (s) => s.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/^\s*\/\/.*$/gm, ' ');
  const html = stripHtmlComments(await (await fetch(BASE + '/')).text());
  const domIds = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
  const domMissing = new Set();
  for (const name of ['/app.js', '/eval.js']) {
    const src = stripJsComments(await (await fetch(BASE + name)).text());
    for (const m of src.matchAll(/\$\('([A-Za-z0-9_-]+)'\)/g)) {
      if (!domIds.has(m[1])) domMissing.add(`${name} → #${m[1]}`);
    }
  }
  if (domMissing.size) {
    console.error('DOM_CONTRACT_FAILED（JS 引用了不存在的元素 id）:\n  - '
      + [...domMissing].join('\n  - '));
    process.exit(3);
  }
  process.stderr.write(`DOM 引用检查通过（${domIds.size} 个 id 全部存在）\n`);

  const target = await pickTarget();
  const c = makeClient(target.webSocketDebuggerUrl);
  await c.ready;
  await c.send('Runtime.enable');
  await c.send('Log.enable');
  await c.send('Page.enable');
  await c.send('Network.enable');

  const errors = [], cErrs = [], badReqs = [];
  c.on((m) => {
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails;
      errors.push(d.exception?.description || d.text);
    }
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
      cErrs.push((m.params.args || []).map((a) => a.value ?? a.description).join(' '));
    }
    if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') cErrs.push(m.params.entry.text);
    if (m.method === 'Network.responseReceived') {
      const r = m.params.response;
      if (r.status >= 400) badReqs.push(`${r.status} ${r.url}`);
    }
  });

  await c.send('Page.navigate', { url: BASE });
  await sleep(6500);

  const evalJs = async (expr) =>
    (await c.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })).result.value;
  const shot = async (name) => {
    const s = await c.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    fs.writeFileSync(`${OUTDIR}/${name}.png`, Buffer.from(s.data, 'base64'));
  };
  const goto = async (p) => {
    await evalJs(`document.querySelector('#tabs button[data-panel="${p}"]').click()`);
    await sleep(1800);
  };
  const report = {};

  /* ---------- 1. 全部 tab 巡检 ---------- */
  const PANELS = ['dashboard', 'connections', 'benchmark', 'eval', 'loadtest', 'history', 'alerts'];
  report.panels = {};
  for (const p of PANELS) {
    await goto(p);
    report.panels[p] = await evalJs(`({
      active: !!document.querySelector('#panel-${p}.active'),
      chartOnCanvas: Array.from(document.querySelectorAll('#panel-${p} .chart'))
        .filter(el => window.echarts && echarts.getInstanceByDom(el)).length,
      chartCount: document.querySelectorAll('#panel-${p} .chart').length,
      zeroSize: Array.from(document.querySelectorAll('#panel-${p} .chart'))
        .filter(el => el.clientWidth === 0 || el.clientHeight === 0).length,
    })`);
    await shot('panel-' + p);
  }

  /* ---------- 2. 评测中心：静态渲染断言 ---------- */
  await goto('eval');
  await sleep(1800);
  report.evalStatic = await evalJs(`({
    suites: document.querySelectorAll('#evSuiteList .ev-suite').length,
    groups: document.querySelectorAll('#evSuiteList .ev-group').length,
    catalogMetrics: document.querySelectorAll('#evCatalog .ev-metric').length,
    catalogCats: document.querySelectorAll('#evCatalog .ev-cat').length,
    coverHint: document.getElementById('evCoverHint').innerText,
    modeOptions: Array.from(document.getElementById('evMode').options).map(o => o.value),
    connOptions: document.getElementById('evConn').options.length,
    modelOptions: Array.from(document.getElementById('evModel').options).map(o => o.text),
    judgeHasEmpty: document.getElementById('evJudge').options[0].value === '',
    effText: document.getElementById('evEff').innerText.slice(0, 500),
    selectedSuite: (document.querySelector('#evSuiteList .ev-suite.sel') || {}).innerText,
    suiteDesc: document.getElementById('evSuiteDesc').innerText.slice(0, 160),
    estimate: document.getElementById('evEstimate').innerText,
  })`);
  await shot('eval-static');

  /* ---------- 3. 效率指标 & 冷启动 ---------- */
  await evalJs(`document.getElementById('btnEffReload').click()`);
  await sleep(2500);
  report.efficiency = await evalJs(`({
    items: document.querySelectorAll('#evEff .metric-item').length,
    text: document.getElementById('evEff').innerText.slice(0, 600),
    hasJson: !!document.querySelector('#evEff .ev-json'),
  })`);
  /* 数值一致性断言：效率接口的参数量 / FLOPs / KV 必须自洽。
     这一条是为了拦住「把词表大小当参数量、把隐藏维度当上下文」这类
     位置错填 —— 上一步只 dump 文本时它曾经溜过去过。 */
  report.effNumbers = await evalJs(`(async () => {
    const r = await fetch('/api/metrics/efficiency?conn_id=1&model=demo-qwen-7b');
    const d = await r.json();
    const m = d.model || {}, f = d.flops || {}, k = d.kv_cache || {};
    const checks = [];
    const ok = (name, pass, got) => checks.push({ name, pass: !!pass, got });
    ok('params 是 76 亿量级(非词表 152064)', m.params > 1e9 && m.params < 2e10, m.params);
    ok('params_b 与 params 一致', Math.abs((m.params_b || 0) * 1e9 - (m.params || 0)) < 1e8,
       m.params_b + ' vs ' + m.params);
    ok('flops_per_token ≈ 2×params', Math.abs((f.flops_per_token || 0) - 2 * (m.params || 0)) < 1e6,
       f.flops_per_token);
    ok('context_length 是 32768(非隐藏维度 3584)', m.context_length === 32768, m.context_length);
    ok('head_dim=128 / n_layer=28 / n_head_kv=4',
       m.head_dim === 128 && m.n_layer === 28 && m.n_head_kv === 4,
       [m.n_layer, m.n_head_kv, m.head_dim].join('/'));
    ok('KV 单槽 ≈448MiB', Math.abs((k.per_slot_mib || 0) - 448) < 2, k.per_slot_mib);
    ok('simulated 已标注', d.simulated === true, d.simulated);
    return { checks, failed: checks.filter(c => !c.pass).map(c => c.name) };
  })()`);
  await evalJs(`document.getElementById('btnColdStart').click()`);
  await sleep(4000);
  report.coldStart = await evalJs(`document.getElementById('evCold').innerText.slice(0, 300)`);
  await shot('eval-efficiency');

  /* ---------- 4. 真跑一个评测套件 ---------- */
  await evalJs(`(() => {
    const b = document.querySelector('#evSuiteList .ev-suite[data-suite="${SUITE}"]');
    if (b) b.click();
  })()`);
  await sleep(1500);
  report.beforeRun = await evalJs(`({
    suiteId: (document.querySelector('#evSuiteList .ev-suite.sel') || {}).dataset?.suite,
    runBtnDisabled: document.getElementById('btnRunEval').disabled,
    runBtnText: document.getElementById('btnRunEval').innerText,
    estimate: document.getElementById('evEstimate').innerText,
  })`);

  await evalJs(`document.getElementById('btnRunEval').click()`);
  await sleep(3000);
  report.running = await evalJs(`({
    stopDisabled: document.getElementById('btnStopEval').disabled,
    progress: document.getElementById('evProgress').innerText,
  })`);
  await shot('eval-running');

  let finished = false;
  for (let i = 0; i < 30; i++) {
    await sleep(2500);
    const txt = await evalJs(`document.getElementById('evProgress').innerText`);
    if (/完成|失败|停止/.test(txt || '')) { finished = true; break; }
  }
  report.finished = finished;
  report.evalDone = await evalJs(`({
    hint: document.getElementById('evResultHint').innerText,
    kpis: document.querySelectorAll('#evSummary .kpi').length,
    kpiText: Array.from(document.querySelectorAll('#evSummary .kpi')).map(
      el => el.querySelector('.label').innerText + '=' + el.querySelector('.value').innerText.trim()).slice(0, 14),
    metricItems: document.querySelectorAll('#evSummary .metric-item').length,
    itemRows: document.querySelectorAll('#evItemTable tr').length,
    itemText: document.getElementById('evItemTable').innerText.slice(0, 420),
    exportEnabled: !document.getElementById('btnEvCsv').disabled,
    reportEnabled: !document.getElementById('btnEvReport').disabled,
  })`);
  await shot('eval-done');

  /* ---------- 5. 报告弹窗 ---------- */
  await evalJs(`document.getElementById('btnEvReport').click()`);
  await sleep(2200);
  report.reportModal = await evalJs(`({
    shown: document.getElementById('modalMask').classList.contains('show'),
    len: document.getElementById('modalBody').innerText.length,
    text: document.getElementById('modalBody').innerText.slice(0, 400),
  })`);
  await shot('eval-report-modal');
  await evalJs(`document.getElementById('modalClose').click()`);

  /* ---------- 6. 历史面板应能看到 eval 记录 ---------- */
  await goto('history');
  await sleep(1500);
  report.history = await evalJs(`({
    rows: document.querySelectorAll('#histTable tr').length,
    text: document.getElementById('histTable').innerText.slice(0, 460),
  })`);

  /* ---------- 7. 响应式：窄视口再看一眼评测面板 ---------- */
  await c.send('Emulation.setDeviceMetricsOverride', {
    width: 1366, height: 900, deviceScaleFactor: 1, mobile: false,
  });
  await goto('eval');
  await sleep(2000);
  report.narrow = await evalJs(`({
    innerWidth: window.innerWidth,
    kpiPerRow: (() => {
      const row = document.querySelector('#evSummary .row.r4');
      if (!row) return 0;
      const kids = Array.from(row.children);
      const top = kids[0].getBoundingClientRect().top;
      return kids.filter(k => Math.abs(k.getBoundingClientRect().top - top) < 2).length;
    })(),
    overflowX: document.documentElement.scrollWidth > window.innerWidth + 2,
  })`);
  await shot('eval-narrow');
  await c.send('Emulation.clearDeviceMetricsOverride');

  report.uncaughtExceptions = errors;
  report.consoleErrors = cErrs;
  report.failedRequests = badReqs;

  /* ---------- 8. 断言汇总：任何一条不过就非零退出，避免"看起来跑通了" ---------- */
  const failures = [];
  if (report.effNumbers && report.effNumbers.failed.length) {
    failures.push('效率指标数值自洽: ' + report.effNumbers.failed.join(' / '));
  }
  if (errors.length) failures.push(`未捕获异常 ${errors.length} 条`);
  if (cErrs.length) failures.push(`console.error ${cErrs.length} 条`);
  if (badReqs.length) failures.push(`失败请求 ${badReqs.length} 条`);
  if (report.evalStatic && report.evalStatic.suites === 0) failures.push('套件列表为空');
  if (report.evalDone && report.evalDone.itemRows < 2) failures.push('评测明细表为空');
  report.assertionFailures = failures;

  console.log(JSON.stringify(report, null, 2));
  c.close();
  if (failures.length) {
    console.error('\nASSERT_FAILED:\n  - ' + failures.join('\n  - '));
    process.exit(2);
  }
  process.stderr.write('\nASSERT_OK：全部断言通过\n');
  process.exit(0);
})().catch((e) => { console.error('VERIFY_FAILED', e.message); process.exit(1); });
