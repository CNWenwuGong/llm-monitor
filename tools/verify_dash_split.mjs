/**
 * 验证「实时看板」左右分栏改造：
 *  - 左半屏 = 性能指标看板，右半屏 = 对话测试，两列等宽各占 50%
 *  - 图表在变窄后仍能正常初始化（非 0×0）
 *  - 在看板内直接发一条对话，验证流式打点链路仍然可用
 *  - 全流程无 console error / 未捕获异常 / 资源加载失败
 */
const CDP_HTTP = process.env.CDP_HTTP || 'http://127.0.0.1:9222';
const TARGET_URL = process.env.TARGET_URL || 'http://127.0.0.1:8081';
const OUTDIR = process.env.OUTDIR || '.';
const WIDTH = Number(process.env.VW || 1600);
const HEIGHT = Number(process.env.VH || 1100);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pickTarget() {
  for (let i = 0; i < 40; i++) {
    try {
      const list = await (await fetch(`${CDP_HTTP}/json/list`)).json();
      const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch (e) { /* chrome 未就绪 */ }
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
    ws.onerror = () => rej(new Error('WebSocket 连接失败'));
  });
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
    } else if (msg.method) listeners.forEach((fn) => fn(msg));
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

const PROBE = `(() => {
  const t = (id) => ((document.getElementById(id) || {}).textContent || '').trim();
  const split = document.querySelector('.dash-split');
  const cols = split ? Array.from(split.children) : [];
  const box = (el) => { const r = el.getBoundingClientRect(); return { x: Math.round(r.x), w: Math.round(r.width), h: Math.round(r.height) }; };
  const charts = Array.from(document.querySelectorAll('#panel-dashboard .chart'));
  return {
    tabs: Array.from(document.querySelectorAll('#tabs button')).map((b) => b.textContent.trim()),
    hasChatPanel: !!document.getElementById('panel-chat'),
    splitCols: cols.length,
    splitWidth: split ? Math.round(split.getBoundingClientRect().width) : 0,
    colRects: cols.map(box),
    colRatio: cols.length === 2 && split
      ? +(cols[0].getBoundingClientRect().width / split.getBoundingClientRect().width).toFixed(3) : null,
    kpiCount: document.querySelectorAll('#panel-dashboard .r4 .kpi').length,
    kpiValues: Array.from(document.querySelectorAll('#panel-dashboard .kpi .value')).map((e) => e.textContent.trim()),
    kpiPerRow: (() => {
      const rows = Array.from(document.querySelectorAll('#panel-dashboard .row.r4'));
      return rows.map((r) => getComputedStyle(r).gridTemplateColumns.split(' ').length);
    })(),
    chartSizes: charts.map((el) => {
      const i = window.echarts ? echarts.getInstanceByDom(el) : null;
      const r = el.getBoundingClientRect();
      return (i ? 'init' : 'none') + ':' + Math.round(r.width) + 'x' + Math.round(r.height);
    }),
    chatBox: (() => { const b = document.getElementById('chatBox'); if (!b) return null; return box(b); })(),
    chatBoxInRightCol: (() => {
      const b = document.getElementById('chatBox'); if (!b || cols.length < 2) return null;
      return b.getBoundingClientRect().x > cols[1].getBoundingClientRect().x - 2;
    })(),
    chatConnOptions: Array.from((document.getElementById('chatConn') || {}).options || []).map((o) => o.text),
    chatModelOptions: Array.from((document.getElementById('chatModel') || {}).options || []).map((o) => o.text),
    chatConnValue: (document.getElementById('chatConn') || {}).value,
    gpuBadge: t('gpuBadge'),
    logLines: document.querySelectorAll('#dashLog div').length,
  };
})()`;

const CHAT_PROBE = `(() => {
  const t = (id) => ((document.getElementById(id) || {}).textContent || '').trim();
  const msgs = Array.from(document.querySelectorAll('#chatBox .msg'));
  const ai = msgs.filter((m) => m.classList.contains('ai')).pop();
  return {
    msgCount: msgs.length,
    aiText: ai ? (ai.textContent || '').trim().slice(0, 90) : '',
    aiTextLen: ai ? (ai.querySelector('.text') || {}).textContent?.length || 0 : 0,
    chatStatus: t('chatStatus'),
    mTtft: t('m-ttft'), mE2e: t('m-e2e'), mTps: t('m-tps'),
    mPt: t('m-pt'), mCt: t('m-ct'), mGent: t('m-gent'),
    util: t('m-utilTxt'), runId: t('chatRunId'),
    sendDisabled: (document.getElementById('btnSend') || {}).disabled,
    before: null,
  };
})()`;

(async () => {
  const target = await pickTarget();
  const c = makeClient(target.webSocketDebuggerUrl);
  await c.ready;

  const errors = [], consoleErrors = [], warnings = [], failedRequests = [];
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
  await c.send('Emulation.setDeviceMetricsOverride',
    { width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: false });

  await c.send('Page.navigate', { url: TARGET_URL });
  await sleep(7000);

  const fs = await import('node:fs');
  const ev = async (expr) => (await c.send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.value;
  const shot = async (name) => {
    const s = await c.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    fs.writeFileSync(`${OUTDIR}/${name}.png`, Buffer.from(s.data, 'base64'));
  };

  const layout = await ev(PROBE);
  await shot(`split-${WIDTH}x${HEIGHT}`);

  // ---- 在看板右半屏里真实发一条对话（默认连接 1 = 内置演示后端，秒回） ----
  await ev(`(() => {
    const ta = document.getElementById('chatInput');
    ta.value = '用一句话解释什么是首 Token 延迟（TTFT）。';
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    document.getElementById('btnSend').click();
    return true;
  })()`);
  await sleep(2500);
  const midStream = await ev(CHAT_PROBE);
  await sleep(9000);
  const afterChat = await ev(CHAT_PROBE);
  await shot('split-chat-done');

  console.log(JSON.stringify({
    viewport: `${WIDTH}x${HEIGHT}`,
    layout,
    midStream,
    afterChat,
    uncaughtExceptions: errors,
    consoleErrors,
    consoleWarnings: warnings.slice(0, 10),
    failedRequests: failedRequests.filter((f) => !/favicon/i.test(f)).slice(0, 10),
    screenshots: [`${OUTDIR}/split-${WIDTH}x${HEIGHT}.png`, `${OUTDIR}/split-chat-done.png`],
  }, null, 2));
  c.close();
  process.exit(0);
})().catch((e) => { console.error('VERIFY_FAILED', e.message); process.exit(1); });
