/**
 * 用 Chrome DevTools Protocol 验证前端页面：
 *  1. 收集 console error / 未捕获异常 / 网络失败
 *  2. 读取关键 DOM 状态，确认数据已渲染
 *  3. 截图保存
 * 仅依赖 Node 22 内置的 fetch 与 WebSocket，无需第三方包。
 */
const CDP_HTTP = process.env.CDP_HTTP || 'http://127.0.0.1:9222';
const TARGET_URL = process.env.TARGET_URL || 'http://127.0.0.1:8080';
const SHOT = process.env.SHOT || 'shot.png';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pickTarget() {
  for (let i = 0; i < 40; i++) {
    try {
      const list = await (await fetch(`${CDP_HTTP}/json/list`)).json();
      const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch (e) { /* chrome 还没起来 */ }
    await sleep(500);
  }
  throw new Error('无法连接到 Chrome CDP');
}

function makeClient(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  const listeners = [];
  const ready = new Promise((res, rej) => {
    ws.onopen = () => res();
    ws.onerror = (e) => rej(new Error('WebSocket 连接失败'));
  });
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
    } else if (msg.method) {
      listeners.forEach((fn) => fn(msg));
    }
  };
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const mid = ++id;
    pending.set(mid, { resolve, reject });
    ws.send(JSON.stringify({ id: mid, method, params }));
    setTimeout(() => {
      if (pending.has(mid)) { pending.delete(mid); reject(new Error(`${method} 超时`)); }
    }, 30000);
  });
  return { ready, send, on: (fn) => listeners.push(fn), close: () => ws.close() };
}

(async () => {
  const target = await pickTarget();
  const c = makeClient(target.webSocketDebuggerUrl);
  await c.ready;

  const errors = [], warnings = [], consoleErrors = [], failedRequests = [];

  c.on((msg) => {
    if (msg.method === 'Runtime.exceptionThrown') {
      const d = msg.params.exceptionDetails;
      errors.push(d.exception?.description || d.text);
    } else if (msg.method === 'Runtime.consoleAPICalled') {
      const text = (msg.params.args || []).map((a) => a.value ?? a.description ?? a.type).join(' ');
      if (msg.params.type === 'error') consoleErrors.push(text);
      else if (msg.params.type === 'warning') warnings.push(text);
    } else if (msg.method === 'Log.entryAdded') {
      const e = msg.params.entry;
      if (e.level === 'error') consoleErrors.push(`[${e.source}] ${e.text}`);
      else if (e.level === 'warning') warnings.push(`[${e.source}] ${e.text}`);
    } else if (msg.method === 'Network.loadingFailed') {
      failedRequests.push(`${msg.params.type} ${msg.params.errorText}`);
    }
  });

  await c.send('Runtime.enable');
  await c.send('Log.enable');
  await c.send('Page.enable');
  await c.send('Network.enable');

  if (process.env.NAVIGATE !== '0') {
    await c.send('Page.navigate', { url: TARGET_URL });
  }
  await sleep(6500); // 等待 WS 首帧 + 首屏图表渲染

  const probe = `(() => {
    const t = (id) => (document.getElementById(id) || {}).textContent || '';
    return {
      title: document.title,
      wsStatus: t('wsText'),
      host: t('hostInfo'),
      kpiTps: t('v-tps'), kpiTtft: t('v-ttft'), kpiGpu: t('v-gpu'), kpiVram: t('v-vram'),
      kpiCpu: t('v-cpu'), kpiRam: t('v-ram'), kpiActive: t('v-active'),
      badges: t('badgeReqs') + ' | ' + t('badgeTokens') + ' | ' + t('badgeErrors'),
      connRows: document.querySelectorAll('#connTable tr').length,
      connFirst: (document.querySelector('#connTable tr') || {}).textContent || '',
      nativeRows: document.querySelectorAll('#nativeMetrics .metric-item').length,
      sysRows: document.querySelectorAll('#sysInfo .metric-item').length,
      logLines: document.querySelectorAll('#dashLog div').length,
      canvases: document.querySelectorAll('canvas').length,
      echartsCharts: Array.from(document.querySelectorAll('#panel-dashboard .chart'))
        .filter((el) => !!(window.echarts && echarts.getInstanceByDom(el))).length,
      suiteOptions: (document.getElementById('benchSuite') || {}).length || 0,
      connOptions: (document.getElementById('chatConn') || {}).length || 0,
      echartsLoaded: typeof window.echarts !== 'undefined',
    };
  })()`;
  const res = await c.send('Runtime.evaluate', { expression: probe, returnByValue: true });
  const dom = res.result.value;

  const diag = await c.send('Runtime.evaluate', {
    expression: `JSON.stringify({errors: [], note: 'ok'})`, returnByValue: true,
  });
  void diag;
  void dom;

  const shot = await c.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
  const fs = await import('node:fs');
  fs.writeFileSync(SHOT, Buffer.from(shot.data, 'base64'));

  const out = {
    dom,
    uncaughtExceptions: errors,
    consoleErrors,
    consoleWarnings: warnings.slice(0, 12),
    failedRequests: failedRequests.filter((f) => !/favicon/i.test(f)).slice(0, 12),
    screenshot: SHOT,
  };
  console.log(JSON.stringify(out, null, 2));
  c.close();
  process.exit(0);
})().catch((e) => { console.error('VERIFY_FAILED', e.message); process.exit(1); });
