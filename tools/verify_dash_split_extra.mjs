/**
 * 补充验证：
 *  1. 等对话流式跑完，确认「本次请求指标」卡被真正填满
 *  2. 切到历史对比面板，勾选两条记录做对比，确认 containLabel 改动后图表仍正常
 *  3. 切换到其余面板，确认无异常
 */
const CDP_HTTP = process.env.CDP_HTTP || 'http://127.0.0.1:9222';
const TARGET_URL = process.env.TARGET_URL || 'http://127.0.0.1:8081';
const OUTDIR = process.env.OUTDIR || '.';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pickTarget() {
  const list = await (await fetch(`${CDP_HTTP}/json/list`)).json();
  const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
  if (!page) throw new Error('无可用 page target');
  return page;
}
function makeClient(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0; const pending = new Map();
  const listeners = [];
  const ready = new Promise((res, rej) => { ws.onopen = () => res(); ws.onerror = () => rej(new Error('ws fail')); });
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.id && pending.has(m.id)) {
      const { resolve, reject } = pending.get(m.id); pending.delete(m.id);
      m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
    } else if (m.method) listeners.forEach((fn) => fn(m));
  };
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const mid = ++id; pending.set(mid, { resolve, reject });
    ws.send(JSON.stringify({ id: mid, method, params }));
    setTimeout(() => { if (pending.has(mid)) { pending.delete(mid); reject(new Error(method + ' 超时')); } }, 30000);
  });
  return { ready, send, on: (fn) => listeners.push(fn), close: () => ws.close() };
}

(async () => {
  const t = await pickTarget();
  const c = makeClient(t.webSocketDebuggerUrl);
  await c.ready;
  const errs = [], cerr = [];
  c.on((m) => {
    if (m.method === 'Runtime.exceptionThrown') errs.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    else if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') cerr.push((m.params.args || []).map((a) => a.value ?? a.description).join(' '));
    else if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') cerr.push('[' + m.params.entry.source + '] ' + m.params.entry.text);
  });
  await c.send('Runtime.enable'); await c.send('Log.enable'); await c.send('Page.enable'); await c.send('Network.enable');
  await c.send('Emulation.setDeviceMetricsOverride', { width: 1600, height: 1100, deviceScaleFactor: 1, mobile: false });
  await c.send('Page.navigate', { url: TARGET_URL });
  await sleep(6500);

  const fs = await import('node:fs');
  const ev = async (e) => (await c.send('Runtime.evaluate', { expression: e, returnByValue: true })).result.value;
  const shot = async (n) => {
    const s = await c.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    fs.writeFileSync(`${OUTDIR}/${n}.png`, Buffer.from(s.data, 'base64'));
  };

  // ---- 1. 发一条对话，等到流式结束 ----
  await ev(`(() => {
    const ta = document.getElementById('chatInput');
    ta.value = '用三句话说明本地大模型推理的显存瓶颈。';
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    document.getElementById('btnSend').click(); return true;
  })()`);

  let chat = null;
  for (let i = 0; i < 40; i++) {
    await sleep(2000);
    chat = await ev(`(() => {
      const t = (id) => ((document.getElementById(id) || {}).textContent || '').trim();
      return { status: t('chatStatus'), ttft: t('m-ttft'), e2e: t('m-e2e'), tps: t('m-tps'),
               pt: t('m-pt'), ct: t('m-ct'), gent: t('m-gent'), util: t('m-utilTxt'),
               runId: t('chatRunId'), sendDisabled: (document.getElementById('btnSend')||{}).disabled,
               rows: document.querySelectorAll('#chatBox .msg').length };
    })()`);
    if (/完成|结束|失败|已停止/.test(chat.status)) break;
  }
  await shot('chat-metrics-filled');

  // ---- 2. 历史对比面板：勾两条做对比 ----
  await ev(`document.querySelector('#tabs button[data-panel="history"]').click()`);
  await sleep(2500);
  const hist = await ev(`(() => {
    const cbs = Array.from(document.querySelectorAll('#histTable input[type=checkbox]'));
    cbs.slice(0, 2).forEach((b) => { b.checked = true; b.dispatchEvent(new Event('change', { bubbles: true })); });
    document.getElementById('btnCompareRuns').click();
    return { rows: document.querySelectorAll('#histTable tr').length, checked: cbs.filter((b) => b.checked).length };
  })()`);
  await sleep(2500);
  const histChart = await ev(`(() => {
    const el = document.getElementById('chart-hist');
    const i = window.echarts ? echarts.getInstanceByDom(el) : null;
    const r = el.getBoundingClientRect();
    return { init: !!i, size: Math.round(r.width) + 'x' + Math.round(r.height),
             canvasCount: document.querySelectorAll('#chart-hist canvas').length };
  })()`);
  await shot('history-compare');

  // ---- 3. 其余面板扫一遍 ----
  const panels = ['connections', 'benchmark', 'loadtest', 'alerts', 'dashboard'];
  const panelProbe = {};
  for (const p of panels) {
    await ev(`document.querySelector('#tabs button[data-panel="${p}"]').click()`);
    await sleep(1600);
    panelProbe[p] = await ev(`(() => {
      const el = document.getElementById('panel-${p}');
      const vis = getComputedStyle(el).display !== 'none';
      const charts = Array.from(el.querySelectorAll('.chart')).map((x) => {
        const i = window.echarts ? echarts.getInstanceByDom(x) : null;
        const r = x.getBoundingClientRect();
        return (i ? 'init' : 'none') + ':' + Math.round(r.width) + 'x' + Math.round(r.height);
      });
      return { visible: vis, charts, cards: el.querySelectorAll('.card').length };
    })()`);
  }
  await shot('dashboard-final');

  // ---- 4. 窄屏 1300 ----
  await c.send('Emulation.setDeviceMetricsOverride', { width: 1300, height: 950, deviceScaleFactor: 1, mobile: false });
  await sleep(2200);
  const narrow = await ev(`(() => {
    const split = document.querySelector('.dash-split');
    const cols = Array.from(split.children).map((el) => Math.round(el.getBoundingClientRect().width));
    const rows = Array.from(document.querySelectorAll('#panel-dashboard .row.r4'))
      .map((r) => getComputedStyle(r).gridTemplateColumns.split(' ').length);
    return { cols, splitW: Math.round(split.getBoundingClientRect().width), kpiPerRow: rows,
             chatBoxW: Math.round(document.getElementById('chatBox').getBoundingClientRect().width) };
  })()`);
  await shot('narrow-1300');

  console.log(JSON.stringify({ chat, hist, histChart, panelProbe, narrow, uncaughtExceptions: errs, consoleErrors: cerr }, null, 2));
  c.close();
  process.exit(0);
})().catch((e) => { console.error('FAILED', e.message); process.exit(1); });
