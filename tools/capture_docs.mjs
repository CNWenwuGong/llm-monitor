/**
 * 文档截图采集：把 7 个功能页面按**统一的视口与真实操作状态**截成 README 用的图。
 *
 * 与 verify_*.mjs 的区别：那些脚本的目的是「断言」，截图只是副产品（跑到哪截到哪）；
 * 这支脚本的目的是「出图」，所以它会把每个页面都先跑到有数据的状态
 * （真发一轮对话、真跑基准套件、真跑评测、真跑阶梯压测、真做历史对比），
 * 再统一截图，保证 README 里那一排图风格一致、内容饱满。
 *
 *   CDP_HTTP=http://127.0.0.1:9224 TARGET_URL=http://127.0.0.1:8099 \
 *     node tools/capture_docs.mjs
 *
 * 输出：OUTDIR（默认 docs/screenshots）下的 *.png + stdout 的纯 JSON 清单。
 */
const CDP_HTTP = process.env.CDP_HTTP || 'http://127.0.0.1:9222';
const BASE = process.env.TARGET_URL || 'http://127.0.0.1:8080';
const OUTDIR = process.env.OUTDIR || 'docs/screenshots';
const VIEW_W = Number(process.env.VIEW_W || 1600);
const VIEW_H = Number(process.env.VIEW_H || 1000);
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
    setTimeout(() => { if (pending.has(mid)) { pending.delete(mid); reject(new Error(method + ' timeout')); } }, 120000);
  });
  return { ready, send, on: (f) => listeners.push(f), close: () => ws.close() };
}

const log = (...a) => process.stderr.write(a.join(' ') + '\n');

(async () => {
  const fs = await import('node:fs');
  fs.mkdirSync(OUTDIR, { recursive: true });

  const target = await pickTarget();
  const c = makeClient(target.webSocketDebuggerUrl);
  await c.ready;
  await c.send('Runtime.enable');
  await c.send('Log.enable');
  await c.send('Page.enable');

  const errors = [];
  c.on((m) => {
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails;
      errors.push(d.exception?.description || d.text);
    }
  });

  /* 统一视口：不依赖 Chrome 的 --window-size，避免不同机器出来的图宽度不一致 */
  await c.send('Emulation.setDeviceMetricsOverride', {
    width: VIEW_W, height: VIEW_H, deviceScaleFactor: 1, mobile: false,
  });
  await c.send('Page.navigate', { url: BASE });
  await sleep(7000);

  const evalJs = async (expr) =>
    (await c.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })).result.value;

  const shots = [];
  const shoot = async (name, { full = true } = {}) => {
    // 整页截图前必须回到顶部：顶栏是 fixed，页面滚动时 captureBeyondViewport
    // 会把顶栏渲染在页面中部，出来一条「重影」的导航条。
    if (full) { await evalJs(`window.scrollTo(0, 0)`); await sleep(400); }
    const s = await c.send('Page.captureScreenshot', {
      format: 'png', captureBeyondViewport: full,
    });
    const p = `${OUTDIR}/${name}.png`;
    fs.writeFileSync(p, Buffer.from(s.data, 'base64'));
    shots.push({ file: p, bytes: Math.round(Buffer.from(s.data, 'base64').length / 1024) });
    log(`  截图 → ${p} (${shots[shots.length - 1].bytes} KB)`);
  };
  const goto = async (p) => {
    await evalJs(`document.querySelector('#tabs button[data-panel="${p}"]').click()`);
    await sleep(1800);
  };
  const setVal = (id, v) => evalJs(`(() => {
    const el = document.getElementById('${id}');
    el.value = ${JSON.stringify(String(v))};
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    return el.value;
  })()`);
  /** 轮询页面表达式，直到返回真值或超时 */
  const waitFor = async (expr, ms, label) => {
    const t0 = Date.now();
    for (;;) {
      if (await evalJs(expr)) return true;
      if (Date.now() - t0 > ms) { log(`  ! 超时：${label}`); return false; }
      await sleep(1500);
    }
  };
  /** 「点按钮触发的长任务」必须两段式等待：先等按钮 disabled（跑起来了），
      再等它恢复。只等第二段会在点击后立刻返回，截到「还没开始/结果为空」的中间态。 */
  const waitTask = async (btnId, ms, label) => {
    const started = await waitFor(`document.getElementById('${btnId}').disabled === true`, 20000, label + ' 启动');
    const done = await waitFor(`document.getElementById('${btnId}').disabled === false`, ms, label + ' 结束');
    return started && done;
  };

  const report = { viewport: `${VIEW_W}x${VIEW_H}`, shots };

  /* ---------- 0. 把对话面板接到内置演示后端 ---------- */
  log('· 选择内置演示后端');
  report.conn = await evalJs(`(() => {
    const s = document.getElementById('chatConn');
    const opt = Array.from(s.options).find(o => /演示|demo/i.test(o.textContent)) || s.options[0];
    if (!opt) return null;
    s.value = opt.value; s.dispatchEvent(new Event('change', {bubbles:true}));
    return { id: s.value, text: opt.textContent };
  })()`);
  const modelReady = await waitFor(
    `document.getElementById('chatModel') && document.getElementById('chatModel').options.length > 0`,
    20000, '模型列表加载');
  log(`  连接档案 = ${JSON.stringify(report.conn)}  模型就绪=${modelReady}`);
  report.model = await evalJs(`document.getElementById('chatModel').value`);

  /* 先开一个全新会话：这样无论临时库里已经有什么（比如上一次采集留下的轮次），
     截图里的对话都恰好只有一轮，出图可重复。 */
  await evalJs(`document.getElementById('btnNewChat').click()`);
  await waitFor(`document.querySelectorAll('#chatBox .msg').length === 0`, 15000, '新会话就绪');
  log('  已开新会话（保证对话区只有本轮）');

  /* ---------- 0.1 先放出一条告警，让告警页有真实日志 ----------
     tps_low 平时是关闭的；这里临时开启并设成 100 tok/s（演示后端 7B 档位约 52 tok/s，
     一定会触发），跑完压测后再看日志就有真实记录。临时库，不影响用户数据。 */
  await evalJs(`fetch('/api/alerts/rules', {method:'PUT',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({rules:{tps_low:{enabled:true, value:100}}})}).then(r=>r.json())`);

  /* ---------- 1. 对话：发一轮，既喂饱看板右半屏，也供后面聊天截图 ---------- */
  log('· 发一轮对话（等它自己写完）');
  const PROMPT = '请给出一套评估本地大模型推理性能的落地方法。';
  await evalJs(`(() => {
    const t = document.getElementById('chatInput');
    t.value = ${JSON.stringify(PROMPT)};
    t.dispatchEvent(new Event('input', {bubbles:true}));
    document.getElementById('btnSend').click();
  })()`);
  // 同样两段式：send() 里先 await 了两次网络往返才置 ST.running，
  // 只等「结束」会在还没开始时就读到上一轮的「完成」。
  await waitFor(`window.CHAT.ST.running === true`, 30000, '对话开始生成');
  await waitFor(`window.CHAT.ST.running === false`, 240000, '对话生成结束');
  report.chat1 = await evalJs(`({
    status: document.getElementById('chatStatus').textContent,
    msgs: document.querySelectorAll('#chatBox .msg').length,
    hasCode: document.querySelectorAll('#chatBox .msg.ai pre').length,
    hasTable: document.querySelectorAll('#chatBox .msg.ai table').length,
    bullets: document.querySelectorAll('#chatBox .msg.ai li').length,
    bold: document.querySelectorAll('#chatBox .msg.ai b').length,
    tokens: document.getElementById('m-ct').textContent,
  })`);
  log(`  ${JSON.stringify(report.chat1)}`);

  /* ---------- 2. 实时看板（左性能 + 右对话） ---------- */
  await goto('dashboard');
  await sleep(2500);
  // 流式结束后聊天区停在底部，往回滚一条，让图里同时看得到「提问 + 回答开头」
  await evalJs(`(() => {
    const msgs = Array.from(document.querySelectorAll('#chatBox .msg'));
    const lastUser = msgs.reverse().find(m => m.classList.contains('user'));
    if (lastUser) lastUser.scrollIntoView({ block: 'start' });
  })()`);
  await sleep(800);
  await evalJs(`window.scrollTo(0, 0)`);
  report.kpis = await evalJs(`Array.from(document.querySelectorAll('#panel-dashboard .kpi'))
    .map(k => k.querySelector('.label').innerText.trim() + ' = ' +
              k.querySelector('.value').innerText.trim().replace(/\\s+/g, ''))`);
  await shoot('01-dashboard');

  /* ---------- 3. 连接管理 ---------- */
  await goto('connections');
  await sleep(2000);
  await shoot('02-connections');

  /* ---------- 4. 基准测试：真跑一遍冒烟套件 ---------- */
  log('· 跑基准套件（smoke）');
  await goto('benchmark');
  await sleep(1200);
  await setVal('benchMaxTokens', 160);
  await evalJs(`(() => {
    const s = document.getElementById('benchSuite'); s.value = 'smoke';
    s.dispatchEvent(new Event('change', {bubbles:true}));
    document.getElementById('btnRunBench').click();
  })()`);
  await waitTask('btnRunBench', 300000, '基准套件');
  await sleep(2500);
  await shoot('03-benchmark');

  /* ---------- 5. 评测中心：真跑一个套件 ---------- */
  log('· 跑评测套件（mmlu_mini）');
  await goto('eval');
  await sleep(1500);
  await evalJs(`(() => {
    const m = document.getElementById('evMode');
    if (Array.from(m.options).some(o => o.value === 'quick')) { m.value = 'quick'; m.dispatchEvent(new Event('change', {bubbles:true})); }
  })()`);
  await evalJs(`(() => {
    const b = document.querySelector('#evSuiteList .ev-suite[data-suite="mmlu_mini"]');
    if (b) b.click();
  })()`);
  await sleep(1200);
  await evalJs(`document.getElementById('btnRunEval').click()`);
  await waitFor(`/完成|失败|停止/.test(document.getElementById('evProgress').innerText || '')`,
                300000, '评测跑完');
  await evalJs(`document.getElementById('btnEffReload').click()`);
  await sleep(2000);
  await evalJs(`document.getElementById('btnColdStart').click()`);
  await sleep(3500);
  await evalJs(`window.scrollTo(0, 0)`);
  await sleep(600);
  // 评测中心整页有 3700+ px 高，整页截图在 README 里会缩得看不清 —— 按视口分三屏出图
  await shoot('04-eval', { full: false });

  await evalJs(`(() => {
    const el = document.getElementById('evSuiteList');
    window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 110);
  })()`);
  await sleep(1200);
  await shoot('05-eval-suites', { full: false });

  await evalJs(`(() => {
    const el = document.getElementById('evEff');
    window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 110);
  })()`);
  await sleep(1200);
  await shoot('06-eval-efficiency', { full: false });

  /* ---------- 7. 并发压测：真跑一次阶梯加压 ---------- */
  log('· 跑阶梯压测（1→2→4）');
  await goto('loadtest');
  await sleep(1200);
  await evalJs(`(() => {
    const el = document.getElementById('ltMode');
    el.value = 'stage'; el.dispatchEvent(new Event('change', {bubbles:true}));
  })()`);
  await setVal('ltStartC', 1);
  await setVal('ltMaxC', 4);
  await setVal('ltStageSec', 6);
  await setVal('ltDuration', 0);
  await setVal('ltMaxTokens', 96);
  await evalJs(`document.getElementById('btnStartLt').click()`);
  await waitTask('btnStartLt', 300000, '阶梯压测');
  await sleep(2500);
  await shoot('07-loadtest');

  /* ---------- 8. 历史对比：勾两条出来叠图 ---------- */
  await goto('history');
  await sleep(2500);
  report.history = await evalJs(`({ rows: document.querySelectorAll('#histTable tr').length })`);
  await evalJs(`(() => {
    // 评测类记录没有 avg_tps，勾进来会画出一根 0 高的柱子，看着像坏了 —— 跳过它们
    const rows = Array.from(document.querySelectorAll('#histTable tr'));
    let n = 0;
    for (const tr of rows) {
      if (/评测/.test(tr.innerText)) continue;
      const cb = tr.querySelector('input[type=checkbox]');
      if (!cb) continue;
      cb.checked = true; cb.dispatchEvent(new Event('change', {bubbles:true}));
      if (++n >= 4) break;
    }
    document.getElementById('btnCompareRuns').click();
  })()`);
  await sleep(3000);
  await evalJs(`window.scrollTo(0, 0)`);
  await shoot('08-history');

  /* ---------- 9. 告警阈值 ---------- */
  await goto('alerts');
  await sleep(2000);
  report.alerts = await evalJs(`({ rows: document.querySelectorAll('#alertTable tr').length })`);
  await shoot('09-alerts');

  /* ---------- 10. 对话测试：全屏 + 富文本 ----------
     直接出全屏图。非全屏时右半屏只有几百像素宽，长回答一屏放不下，
     截出来只能看到中段的一句话，不如图个全屏态把「提问 + 富文本回答 + 操作按钮」讲清楚。 */
  await goto('dashboard');
  await sleep(2000);
  await evalJs(`document.getElementById('btnChatExpand').click()`);
  await sleep(1800);
  // 滚到提问处，让图里从问题开始
  await evalJs(`(() => {
    const msgs = Array.from(document.querySelectorAll('#chatBox .msg'));
    const lastUser = msgs.reverse().find(m => m.classList.contains('user'));
    if (lastUser) lastUser.scrollIntoView({ block: 'start' });
  })()`);
  await sleep(1000);
  await shoot('10-chat-rich', { full: false });
  await evalJs(`document.getElementById('btnChatExpand').click()`);
  await sleep(1200);
  await sleep(1000);

  /* ---------- 11. 窄视口下的看板（响应式） ---------- */
  await c.send('Emulation.setDeviceMetricsOverride', {
    width: 1300, height: 900, deviceScaleFactor: 1, mobile: false,
  });
  await goto('dashboard');
  await sleep(2500);
  await shoot('11-dashboard-1300');
  await c.send('Emulation.setDeviceMetricsOverride', {
    width: VIEW_W, height: VIEW_H, deviceScaleFactor: 1, mobile: false,
  });

  report.uncaughtExceptions = errors;
  log(`\n完成：${shots.length} 张，未捕获异常 ${errors.length} 条`);
  console.log(JSON.stringify(report, null, 2));
  c.close();
  process.exit(errors.length ? 2 : 0);
})().catch((e) => { console.error('CAPTURE_FAILED', e.message); process.exit(1); });
