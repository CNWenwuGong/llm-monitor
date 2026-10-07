/**
 * 桌面端（打包后的 exe 窗口）验证。
 *
 *   CDP_HTTP=http://127.0.0.1:9223 OUTDIR=shots node tools/verify_desktop.mjs
 *
 * 为什么能连进去：WebView2 支持官方附加参数环境变量
 *
 *     WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9223
 *
 * 所以启动 exe 前把它设上，就能像普通 Chrome 一样用 CDP 做 DOM 断言 + 截图。
 * 断言的重点不是"服务活着"（那个用 curl /api/health 就够），而是：
 *
 *   1. 窗口里跑的**确实是 WebView2 宿主**（chrome.webview 存在），不是被
 *      系统浏览器接管的页面；
 *   2. 前端在这个宿主里**真的跑起来了**——WebSocket 连上、ECharts 出图，
 *      而不是白屏或停在"连接中…"；
 *   3. 控制台干净，没有打包后才冒出来的资源 404 / 动态导入失败。
 *
 * stdout 是纯 JSON，进度与失败信息走 stderr。
 */
const CDP_HTTP = process.env.CDP_HTTP || 'http://127.0.0.1:9223';
const OUTDIR = process.env.OUTDIR || '.';
const EXPECT_PORT = process.env.EXPECT_PORT || '';   // 留空则不校验端口

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const failures = [];
function check(cond, name, detail) {
  if (cond) process.stderr.write(`  PASS  ${name}\n`);
  else {
    failures.push(name + (detail ? ` << ${detail}` : ''));
    process.stderr.write(`  FAIL  ${name}${detail ? `  << ${detail}` : ''}\n`);
  }
}

async function pickTarget() {
  const list = await (await fetch(`${CDP_HTTP}/json/list`)).json();
  const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
  if (!page) throw new Error('没有可用的 page target（窗口没起来？）');
  return page;
}

function makeClient(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  const listeners = [];
  const ready = new Promise((res, rej) => {
    ws.onopen = () => res();
    ws.onerror = () => rej(new Error('ws fail'));
  });
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
    setTimeout(() => {
      if (pending.has(mid)) { pending.delete(mid); reject(new Error(method + ' timeout')); }
    }, 60000);
  });
  return { ready, send, on: (f) => listeners.push(f), close: () => ws.close() };
}

/** 轮询页面表达式，直到返回真值或超时 */
async function waitFor(c, expr, ms, label) {
  const t0 = Date.now();
  for (;;) {
    const v = await evalJs(c, expr).catch(() => null);
    if (v) return true;
    if (Date.now() - t0 > ms) { process.stderr.write(`  ! 超时：${label}\n`); return false; }
    await sleep(400);
  }
}

async function evalJs(c, expr) {
  const r = await c.send('Runtime.evaluate', {
    expression: `(() => { try { return (${expr}); } catch (e) { return '__ERR__' + e.message; } })()`,
    returnByValue: true, awaitPromise: true,
  });
  return r.result && r.result.value;
}

(async () => {
  const fs = await import('node:fs');

  const target = await pickTarget();
  const c = makeClient(target.webSocketDebuggerUrl);
  await c.ready;

  const consoleErrors = [];
  const uncaught = [];
  c.on((m) => {
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
      const text = (m.params.args || []).map((a) => a.value ?? a.description ?? '').join(' ');
      if (!/favicon/i.test(text)) consoleErrors.push(text.slice(0, 240));
    }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails || {};
      uncaught.push((d.exception && (d.exception.description || d.exception.value)) || d.text || '');
    }
  });

  await c.send('Runtime.enable');
  await c.send('Page.enable');

  /* ---------- 1. 宿主身份：确认跑在 WebView2 壳里 ---------- */
  const url = await evalJs(c, 'location.href');
  process.stderr.write(`  页面: ${url}\n`);
  check(/^http:\/\/127\.0\.0\.1:\d+\//.test(String(url)), '页面 URL 是本机服务', String(url));
  if (EXPECT_PORT) {
    check(String(url).includes(`:${EXPECT_PORT}/`), `端口是 ${EXPECT_PORT}`, String(url));
  }
  // 端口与启动器写下的 runtime.json 应当一致——桌面版刻意用动态端口
  check(await evalJs(c, '!!(window.chrome && window.chrome.webview)'),
        '宿主是 WebView2（chrome.webview 存在）');
  const ua = await evalJs(c, 'navigator.userAgent');
  check(/Edg\//.test(String(ua)), 'UA 是 Edge/WebView2 内核', String(ua).slice(0, 90));

  /* ---------- 2. 重新加载一次，把启动期的控制台错误也抓全 ---------- */
  await c.send('Page.reload', { ignoreCache: true });
  await sleep(1500);

  for (let i = 0; i < 40; i++) {
    const ok = await evalJs(c, 'document.readyState === "complete" && !!document.getElementById("wsStatus")');
    if (ok) break;
    await sleep(500);
  }

  /* ---------- 3. 前端真的跑起来了 ---------- */
  const wsOn = await waitFor(
    c, 'document.getElementById("wsStatus") && document.getElementById("wsStatus").classList.contains("on")',
    30000, 'WebSocket 连上（#wsStatus.on）');
  check(wsOn, 'WebSocket 实时通道已建立');
  check((await evalJs(c, 'document.getElementById("wsText").textContent')) === '实时连接',
        '#wsText 文案是「实时连接」');

  const charts = await waitFor(
    c, 'document.querySelectorAll("#chart-tps canvas, #chart-gpu canvas, #chart-cpu canvas").length >= 3',
    25000, 'ECharts 三个主图画布');
  check(charts, 'ECharts 已在窗口内完成渲染（≥3 个 canvas）');

  const sampleBadge = await evalJs(c, '(document.getElementById("sampleBadge")||{}).textContent || ""');
  check(String(sampleBadge).trim().length > 0, '采样徽标有内容', String(sampleBadge));

  // 看板 KPI 卡确实拿到了数字（说明 WS 推数据 + 渲染链路都通）
  const kpiText = await evalJs(c,
    'Array.from(document.querySelectorAll("#panel-dashboard .kpi")).map(e => e.textContent.trim()).join("|")');
  check(String(kpiText || '').length > 10, '看板 KPI 已填充', String(kpiText).slice(0, 120));

  // 对话面板的 DOM 契约（桌面版最容易因为窗口尺寸变化而出问题的区域）
  const chatOk = await evalJs(c,
    '["chatBox","chatInput","btnSend","chatModel","chatStatus"].every(id => !!document.getElementById(id))');
  check(chatOk, '对话面板关键 DOM 完整（chatBox / chatInput / btnSend / chatModel）');

  /* ---------- 4. 控制台干净 ---------- */
  check(uncaught.length === 0, '无未捕获异常', uncaught.join(' | ').slice(0, 300));
  check(consoleErrors.length === 0, '无 console.error', consoleErrors.join(' | ').slice(0, 300));

  /* ---------- 5. 截图 ---------- */
  fs.mkdirSync(OUTDIR, { recursive: true });
  const shot = await c.send('Page.captureScreenshot', { format: 'png' });
  const path = `${OUTDIR}/desktop-window.png`;
  const buf = Buffer.from(shot.data, 'base64');
  fs.writeFileSync(path, buf);
  process.stderr.write(`  截图 → ${path} (${Math.round(buf.length / 1024)} KB)\n`);

  const viewport = await evalJs(c, '({w: innerWidth, h: innerHeight})');
  const meta = await evalJs(c, '({vram: (document.getElementById("v-vram")||{}).textContent, tabs: document.querySelectorAll("#tabs .tab").length})');

  c.close();
  process.stdout.write(JSON.stringify({
    ok: failures.length === 0,
    url, ua, viewport, sampleBadge, screenshot: path,
    charts: 3, consoleErrors, uncaught, failures,
  }, null, 2) + '\n');
  process.exit(failures.length ? 2 : 0);
})().catch((e) => {
  process.stderr.write('VERIFY_FAILED: ' + (e && e.stack || e) + '\n');
  process.exit(1);
});
