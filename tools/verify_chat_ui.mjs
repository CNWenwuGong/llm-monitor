/**
 * 对话测试面板的无头验证（多轮记忆 / 不截断 / 会话管理 / 记忆面板 / 全屏）。
 *
 *   TARGET_URL=http://127.0.0.1:8099 OUTDIR=shots node tools/verify_chat_ui.mjs
 *
 * stdout 是纯 JSON（可直接管道给 jq / python），日志一律走 stderr。
 */
const CDP_HTTP = process.env.CDP_HTTP || 'http://127.0.0.1:9222';
const BASE = process.env.TARGET_URL || 'http://127.0.0.1:8080';
const OUTDIR = process.env.OUTDIR || '.';
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
  if (!page) throw new Error('没有可用的 page target');
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
    }, 120000);
  });
  return { ready, send, on: (f) => listeners.push(f), close: () => ws.close() };
}

/** 确保存在一个演示后端连接档案，并返回它的 id。
 *  注意：GET /api/connections 返回**裸数组**，POST 只回 {id, ok}。 */
async function ensureDemoConn() {
  const raw = await (await fetch(`${BASE}/api/connections`)).json();
  const conns = Array.isArray(raw) ? raw : (raw.connections || []);
  const demo = conns.find((c) => c.backend === 'demo');
  if (demo && demo.id) return demo;
  const r = await fetch(`${BASE}/api/connections`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name: '验证用演示连接', backend: 'demo', base_url: 'demo://local',
      api_key: '', default_model: 'demo-qwen-7b',
    }),
  });
  const created = await r.json();
  if (!created.id) throw new Error('创建演示连接失败: ' + JSON.stringify(created));
  return { id: created.id, backend: 'demo', default_model: 'demo-qwen-7b' };
}

(async () => {
  const fs = await import('node:fs');

  /* ---------- 0. 前置闸门：服务端返回的 JS 必须能解析 ----------
     这张网兜住过一次真实事故：模板串里嵌三元漏了 else 分支，整个
     eval.js 静默失效。这里在连浏览器之前先把脚本抓下来解析一遍。 */
  const SYNTAX_GATE = ['/app.js', '/chat.js', '/eval.js'];
  const syntaxFailures = [];
  for (const name of SYNTAX_GATE) {
    try {
      const code = await (await fetch(BASE + name)).text();
      new Function(code);            // 仅解析，不执行
    } catch (e) {
      syntaxFailures.push(`${name}: ${e.message}`);
    }
  }
  if (syntaxFailures.length) {
    process.stderr.write('FRONTEND_SYNTAX_FAILED:\n  - ' + syntaxFailures.join('\n  - ') + '\n');
    process.exit(3);
  }
  process.stderr.write(`前端脚本语法检查通过（${SYNTAX_GATE.join(', ')}）\n`);

  /* ---------- 0.1 DOM 契约闸门：JS 里 $('x') 引用的 id 必须真的存在 ----------
     前端改版时删掉/注释掉一段 HTML、却忘了 JS 还在写它，这类问题**不会有语法错**，
     只在运行时抛一次 "Cannot set properties of null"，控制台里一闪而过，极难发现。
     踩过一次：看板双栏改版把 #intervalTxt 注释掉了，app.js 里那句赋值就让每条
     hello 消息都抛一次异常，一直没人注意到。 */
  const stripHtmlComments = (s) => s.replace(/<!--[\s\S]*?-->/g, ' ');
  const stripJsComments = (s) => s.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/^\s*\/\/.*$/gm, ' ');
  const html = stripHtmlComments(await (await fetch(BASE + '/')).text());
  const domIds = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
  const domMissing = new Set();
  for (const name of SYNTAX_GATE) {
    const src = stripJsComments(await (await fetch(BASE + name)).text());
    for (const m of src.matchAll(/\$\('([A-Za-z0-9_-]+)'\)/g)) {
      if (!domIds.has(m[1])) domMissing.add(`${name} → #${m[1]}`);
    }
  }
  if (domMissing.size) {
    process.stderr.write('DOM_CONTRACT_FAILED（JS 引用了不存在的元素 id）:\n  - '
      + [...domMissing].join('\n  - ') + '\n');
    process.exit(3);
  }
  process.stderr.write(`DOM 引用检查通过（${domIds.size} 个 id 全部存在）\n`);

  const demo = await ensureDemoConn();
  process.stderr.write(`演示连接 #${demo.id}\n`);

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

  /* 固定视口：无头 Chrome 默认窗口可能只有 400 出头的高度，
     会让「全屏」「窄屏」这类与尺寸有关的断言测出无意义的值。
     走 CDP 覆盖比 --window-size 可靠（后者常被 headless=new 忽略）。 */
  const VW = Number(process.env.VW || 1600);
  const VH = Number(process.env.VH || 1000);
  await c.send('Emulation.setDeviceMetricsOverride', {
    width: VW, height: VH, deviceScaleFactor: 1, mobile: false,
  });

  await c.send('Page.navigate', { url: BASE });
  await sleep(6500);
  process.stderr.write(`视口 ${VW}×${VH}\n`);

  const evalJs = async (expr) =>
    (await c.send('Runtime.evaluate', {
      expression: expr, returnByValue: true, awaitPromise: true,
    })).result.value;

  /** 轮询等待浏览器侧条件成立（默认 30s）。 */
  const waitFor = async (expr, ms = 30000, label = '') => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      const v = await evalJs(`(() => { try { return !!(${expr}); } catch (e) { return false; } })()`);
      if (v) return true;
      await sleep(300);
    }
    process.stderr.write(`  (waitFor 超时: ${label || expr})\n`);
    return false;
  };

  /**
   * 等一次「按钮驱动」的流式生成完整跑完：先等 running 变 true，再等它变回 false。
   *
   * 为什么不能直接 waitFor('!running')：`继续生成` / `重新生成` 走的是按钮 click，
   * 脚本拿不到 send() 的 promise。而 send() 在把 ST.running 置 true 之前先 await 了
   * 一次网络往返（重新生成要先 DELETE 旧回答），这个空窗期里 running 仍是 false ——
   * 首轮轮询会立刻返回 true，于是读到的正是「旧回答已删、新回答还没生成」的中间态
   * （实测拿到 roles=user,assistant,user、msgCount 5→3 的假失败）。
   */
  const waitRun = async (label, ms = 60000, graceMs = 8000) => {
    const started = await waitFor('window.CHAT.ST.running === true', graceMs, `${label}：进入运行态`);
    if (!started) process.stderr.write(`  (注意：未观察到「${label}」进入运行态，可能已提前完成)\n`);
    return waitFor('!window.CHAT.ST.running', ms, label);
  };

  const shot = async (name) => {
    const s = await c.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    fs.writeFileSync(`${OUTDIR}/${name}.png`, Buffer.from(s.data, 'base64'));
  };

  const report = {};

  /* ---------- 1. 进入看板并准备对话环境 ---------- */
  await evalJs(`document.querySelector('#tabs button[data-panel="dashboard"]').click()`);
  await sleep(1500);
  await evalJs(`(() => {
    const el = document.getElementById('chatConn');
    if (el) { el.value = '${demo.id}'; }
    if (typeof window.loadModelsInto === 'function') window.loadModelsInto('chatModel', ${demo.id});
  })()`);
  await sleep(1200);

  report.static = await evalJs(`({
    hasModule: typeof window.CHAT === 'object' && window.CHAT !== null,
    hasBox: !!document.getElementById('chatBox'),
    hasCtx: !!document.getElementById('chatCtx'),
    hasMemCard: !!document.getElementById('chatMemCard'),
    hasSessionList: !!document.getElementById('chatSessionList'),
    hasUnlimited: !!document.getElementById('chatUnlimited'),
    unlimitedChecked: !!(document.getElementById('chatUnlimited') || {}).checked,
    maxTokDisabled: !!(document.getElementById('chatMaxTokens') || {}).disabled,
    maxTokValue: (document.getElementById('chatMaxTokens') || {}).value || '',
    sessionOptions: (document.getElementById('chatSessionList') || {}).options
      ? document.getElementById('chatSessionList').options.length : 0,
    modelOptions: (document.getElementById('chatModel') || {}).options
      ? document.getElementById('chatModel').options.length : 0,
    ctxText: (document.getElementById('chatCtx') || {}).innerText || '',
  })`);
  check(report.static.hasModule, 'window.CHAT 已导出（顶层 const 不会自动挂 window）');
  check(report.static.hasBox && report.static.hasCtx && report.static.hasMemCard,
        '对话面板 DOM 契约齐全（chatBox / chatCtx / chatMemCard）');
  check(report.static.hasUnlimited, '提供「不限制输出长度」开关');
  check(report.static.unlimitedChecked,
        '★ 默认勾选「不限制回答长度」——不再默认塞一个 1024 上限把回答砍断');
  check(report.static.maxTokDisabled && report.static.maxTokValue === '',
        '不限制时 max_tokens 输入框被禁用且不填数字（否则用户会以为它仍在生效）');
  check(report.static.modelOptions > 0, `模型下拉已加载（${report.static.modelOptions} 项）`);

  /* ---------- 1.5 输入区 composer（参照截图重构） ---------- */
  report.composer = await evalJs(`(() => {
    const c = document.getElementById('chatComposer');
    const ta = document.getElementById('chatInput');
    const send = document.getElementById('btnSend');
    const rect = send ? send.getBoundingClientRect() : null;
    const cs = send ? getComputedStyle(send) : null;
    return {
      hasComposer: !!c,
      inputInside: !!(c && ta && c.contains(ta)),
      sendInside: !!(c && send && c.contains(send)),
      sendSquare: !!(rect && Math.abs(rect.width - rect.height) < 1.5 && rect.width >= 28),
      sendRadius: cs ? cs.borderRadius : '',
      sendLabel: send ? (send.textContent || '').replace(/\\s+/g, '') : '',
      sendBg: cs ? cs.backgroundColor : '',
      modelInside: !!(c && c.contains(document.getElementById('chatModel'))),
      iconBtns: c ? c.querySelectorAll('.icb').length : 0,
      chipExists: !!document.getElementById('btnChipLen'),
      chipText: (document.getElementById('chipLenTx') || {}).textContent || '',
      hintInside: !!(c && c.contains(document.getElementById('chatStatus'))),
      barFlex: !!(c && c.querySelector('.composer-bar')),
    };
  })()`);
  check(report.composer.hasComposer && report.composer.inputInside,
        '输入框改成圆角 composer 容器（textarea 在内）');
  check(report.composer.barFlex && report.composer.hintInside,
        'composer 底部是工具条 + 状态行');
  check(report.composer.sendInside && report.composer.sendSquare
        && /50%|9999px/.test(report.composer.sendRadius),
        `发送按钮为圆形（${report.composer.sendRadius}）`);
  check(report.composer.sendLabel === '发送',
        `圆形发送按钮保留可读文案（textContent=${report.composer.sendLabel || '空'}）`);
  check(report.composer.modelInside, '模型选择已并入输入区工具条');
  check(report.composer.iconBtns >= 2, `工具条含图标按钮（${report.composer.iconBtns} 个）`);
  check(report.composer.chipExists && /不限制/.test(report.composer.chipText),
        `「回答长度」胶囊显示当前策略（${report.composer.chipText}）`);

  // 胶囊下拉与参数面板必须共用同一份真相
  await evalJs(`document.getElementById('btnChipLen').click()`);
  await sleep(300);
  check(await evalJs(`document.getElementById('chipLenPop').classList.contains('open')`),
        '点击胶囊展开下拉菜单');
  await evalJs(`Array.from(document.querySelectorAll('#chipLenPop .cp-item'))
      .find(b => b.dataset.len === '1024').click()`);
  await sleep(350);
  report.chipPick = await evalJs(`({
    unlimited: document.getElementById('chatUnlimited').checked,
    maxTok: document.getElementById('chatMaxTokens').value,
    disabled: document.getElementById('chatMaxTokens').disabled,
    chip: (document.getElementById('chipLenTx') || {}).textContent || '',
    closed: !document.getElementById('chipLenPop').classList.contains('open'),
  })`);
  check(!report.chipPick.unlimited && Number(report.chipPick.maxTok) === 1024
        && !report.chipPick.disabled,
        `胶囊选 1024 → 参数面板同步（max_tokens=${report.chipPick.maxTok}）`);
  check(report.chipPick.closed, '选定后菜单自动收起');
  check(/1,?024/.test(report.chipPick.chip), `胶囊文案同步（${report.chipPick.chip}）`);
  // 复原成「不限制」，后面的用例依赖这个默认值
  await evalJs(`(() => {
    document.getElementById('btnChipLen').click();
    Array.from(document.querySelectorAll('#chipLenPop .cp-item'))
      .find(b => b.dataset.len === '0').click();
  })()`);
  await sleep(350);
  report.chipBack = await evalJs(`({
    unlimited: document.getElementById('chatUnlimited').checked,
    chip: (document.getElementById('chipLenTx') || {}).textContent || '',
    maxTokDisabled: document.getElementById('chatMaxTokens').disabled,
  })`);
  check(report.chipBack.unlimited && /不限制/.test(report.chipBack.chip)
        && report.chipBack.maxTokDisabled,
        '胶囊可切回「不限制」并恢复输入框禁用');

  // 页面上真正加载的渲染器（与 tools/md_render_check.mjs 同源）能处理表格与代码块
  report.mdSample = await evalJs(`(() => {
    const src = '| 指标 | 值 |' + String.fromCharCode(10) + '| --- | --- |'
      + String.fromCharCode(10) + '| TPS | 55.5 |' + String.fromCharCode(10) + String.fromCharCode(10)
      + String.fromCharCode(96,96,96) + 'python' + String.fromCharCode(10)
      + 'print(1)' + String.fromCharCode(10) + String.fromCharCode(96,96,96);
    const html = window.CHAT.mdRender(src);
    const d = document.createElement('div');
    d.innerHTML = html;
    return {
      th: d.querySelectorAll('table th').length,
      td: d.querySelectorAll('table td').length,
      cb: d.querySelectorAll('.cb').length,
      lang: (d.querySelector('.cb-lang') || {}).textContent || '',
      copy: d.querySelectorAll('.cb-copy').length,
      rawPipe: html.indexOf('| --- |') >= 0,
    };
  })()`);
  check(report.mdSample.th === 2 && report.mdSample.td === 2 && !report.mdSample.rawPipe,
        `页面渲染器支持表格（th=${report.mdSample.th} td=${report.mdSample.td}）`);
  check(report.mdSample.cb === 1 && report.mdSample.lang === 'python'
        && report.mdSample.copy === 1,
        `页面渲染器支持带语言标签与复制按钮的代码块（lang=${report.mdSample.lang}）`);

  /* ---------- 2. 新建会话 ---------- */
  await evalJs(`window.CHAT.newSession()`);
  await sleep(1500);
  report.afterNew = await evalJs(`({
    id: window.CHAT.ST.cur && window.CHAT.ST.cur.id,
    turns: window.CHAT.ST.messages.length,
    empty: !!document.querySelector('#chatBox .empty'),
    listHasSelected: !!document.querySelector('#chatSessionList option[selected]'),
  })`);
  check(!!report.afterNew.id, `新建会话成功（#${report.afterNew.id}）`);
  check(report.afterNew.empty, '新会话显示空态提示');

  /* ---------- 3. 第一轮（真实 SSE） ---------- */
  await evalJs(`window.CHAT.send('chat', '17 × 23 等于多少？请给出验算过程。')`);
  const ok1 = await waitFor('!window.CHAT.ST.running', 60000, '第一轮生成完成');
  check(ok1, '第一轮流式生成完成');
  await sleep(1200);

  report.turn1 = await evalJs(`(async () => {
    const r = await fetch('/api/chat/sessions/' + window.CHAT.ST.cur.id).then(x => x.json());
    const last = r.messages[r.messages.length - 1] || {};
    return {
      msgCount: r.messages.length,
      roles: r.messages.map(m => m.role),
      text: last.content || '',
      finish: last.finish_reason,
      truncated: last.truncated,
      ttft: (document.getElementById('m-ttft') || {}).textContent,
      e2e: (document.getElementById('m-e2e') || {}).textContent,
      ct: (document.getElementById('m-ct') || {}).textContent,
      ctx: (document.getElementById('chatCtx') || {}).innerText || '',
      domMsgs: document.querySelectorAll('#chatBox .msg').length,
      capInMetrics: (last.metrics || {}).max_tokens,
    };
  })()`);
  check(report.turn1.msgCount === 2, `第一轮落库 2 条消息（got=${report.turn1.msgCount}）`);
  check(report.turn1.capInMetrics === 0,
        `默认这一轮没有下发长度上限（落库 max_tokens=${report.turn1.capInMetrics}）`);
  check(report.turn1.text.includes('391'), '第一轮回答包含正确结果 391',
        report.turn1.text.slice(0, 50).replace(/\n/g, ' / '));
  check(report.turn1.finish === 'stop', `终止原因为 stop（got=${report.turn1.finish}）`);
  check(report.turn1.truncated === false, '未被标记为截断');
  check(report.turn1.ttft !== '—' && report.turn1.ttft, `面板已回填 TTFT（${report.turn1.ttft}）`);
  check(report.turn1.domMsgs >= 2, `对话区渲染出 ${report.turn1.domMsgs} 条消息气泡`);
  check(/载入\s*1\/1\s*轮/.test(report.turn1.ctx.replace(/\s+/g, ' ')),
        '上下文指示显示「载入 1/1 轮」', report.turn1.ctx.replace(/\n/g, ' ').slice(0, 90));
  await shot('chat-turn1');

  /* ---------- 4. 第二轮：必须依赖记忆 ---------- */
  await evalJs(`window.CHAT.send('chat', '我刚才问的是什么？')`);
  const ok2 = await waitFor('!window.CHAT.ST.running', 60000, '第二轮生成完成');
  check(ok2, '第二轮流式生成完成');
  await sleep(1200);

  report.turn2 = await evalJs(`(async () => {
    const r = await fetch('/api/chat/sessions/' + window.CHAT.ST.cur.id).then(x => x.json());
    const last = r.messages[r.messages.length - 1] || {};
    return {
      msgCount: r.messages.length,
      text: last.content || '',
      ctx: (document.getElementById('chatCtx') || {}).innerText || '',
      domMsgs: document.querySelectorAll('#chatBox .msg').length,
    };
  })()`);
  check(report.turn2.msgCount === 4, `第二轮后共 4 条消息（got=${report.turn2.msgCount}）`);
  check(report.turn2.text.includes('17 × 23'),
        '★ 第二轮能引用第一轮的提问 —— 多轮记忆生效',
        report.turn2.text.slice(0, 60).replace(/\n/g, ' / '));
  check(/载入\s*2\/2\s*轮/.test(report.turn2.ctx.replace(/\s+/g, ' ')),
        '上下文指示随轮次增长（载入 2/2 轮）',
        report.turn2.ctx.replace(/\n/g, ' ').slice(0, 90));
  await shot('chat-turn2');

  /* ---------- 4.5 富文本：Markdown 应该渲染成真元素，而不是原样显示星号 ---------- */
  report.rich = await evalJs(`(() => {
    const nodes = Array.from(document.querySelectorAll('#chatBox .msg.ai .text'));
    const last = nodes[nodes.length - 1];
    if (!last) return { ok: false };
    const txt = String(last.innerText || '');
    return {
      ok: true,
      li: last.querySelectorAll('ol li, ul li').length,
      bold: last.querySelectorAll('b').length,
      para: last.querySelectorAll('p').length,
      hr: last.querySelectorAll('hr').length,
      rawStars: txt.split('**').length - 1,
      rawTicks: txt.split(String.fromCharCode(96)).length - 1,
      titleCount: document.querySelectorAll('#chatBox .msg.ai .text h1, #chatBox .msg.ai .text h2, #chatBox .msg.ai .text h3').length,
      textLen: txt.length,
    };
  })()`);
  check(report.rich.ok && report.rich.li >= 3,
        `回答里的编号列表渲染为真 <li>（${report.rich.li} 个）`);
  check(report.rich.bold >= 1, `**强调** 渲染为 <b>（${report.rich.bold} 处）`);
  check(report.rich.rawStars === 0,
        `气泡正文里不再残留字面星号（${report.rich.rawStars} 处）`);
  check(report.rich.rawTicks === 0,
        `气泡正文里不再残留字面反引号（${report.rich.rawTicks} 处）`);
  check(report.rich.textLen > 200, `富文本渲染后正文完整可读（${report.rich.textLen} 字）`);
  await shot('chat-rich');

  /* ---------- 5. 继续生成（不截断的兜底） ---------- */
  const beforeCont = await evalJs(`window.CHAT.ST.messages.length`);
  await evalJs(`(async () => {
    const btns = Array.from(document.querySelectorAll('#chatBox button.act'))
      .filter(b => b.dataset.act === 'cont');
    btns[btns.length - 1].click();
  })()`);
  const okCont = await waitRun('继续生成完成');
  await sleep(1200);
  check(okCont, '「继续生成」执行完成');
  report.continue = await evalJs(`(async () => {
    const r = await fetch('/api/chat/sessions/' + window.CHAT.ST.cur.id).then(x => x.json());
    const last = r.messages[r.messages.length - 1] || {};
    return {
      msgCount: r.messages.length,
      role: last.role,
      continued: !!last.continued,
      text: last.content || '',
      domCont: document.querySelectorAll('#chatBox .msg.cont').length,
      userRows: r.messages.filter(m => m.role === 'user').length,
    };
  })()`);
  check(report.continue.msgCount === beforeCont + 1,
        `续写追加 1 条消息（${beforeCont} → ${report.continue.msgCount}）`);
  check(report.continue.role === 'assistant' && report.continue.continued === true,
        '续写消息被标记为 continued');
  check(report.continue.userRows === 2, `续写不新增 user 轮（got=${report.continue.userRows}）`);
  check(report.continue.domCont >= 1, '续写气泡带 cont 样式（视觉上与普通回答区分）');
  check(!report.continue.text.trim().endsWith('，'),
        '续写结尾落在句子边界上（不出半句）',
        JSON.stringify(report.continue.text.slice(-24)));
  await shot('chat-continue');

  /* ---------- 6. 重新生成：不产生重复 user 轮 ---------- */
  const beforeRegen = await evalJs(`window.CHAT.ST.messages.length`);
  await evalJs(`(async () => {
    const btns = Array.from(document.querySelectorAll('#chatBox button.act'))
      .filter(b => b.dataset.act === 'regen');
    btns[btns.length - 1].click();
  })()`);
  const okRegen = await waitRun('重新生成完成');
  await sleep(1200);
  check(okRegen, '「重新生成」执行完成');
  report.regen = await evalJs(`(async () => {
    const r = await fetch('/api/chat/sessions/' + window.CHAT.ST.cur.id).then(x => x.json());
    const ms = r.messages;
    const us = ms.filter(m => m.role === 'user');
    const last = ms[ms.length - 1] || {};
    let dup = 0, adjA = 0;
    for (let i = 1; i < us.length; i += 1) if (us[i].content === us[i-1].content) dup += 1;
    for (let i = 1; i < ms.length; i += 1) {
      if (ms[i].role === 'assistant' && ms[i-1].role === 'assistant') adjA += 1;
    }
    return {
      msgCount: ms.length, userRows: us.length, dupUser: dup, adjAssistant: adjA,
      text: last.content || '', lastRole: last.role,
      continued: !!last.continued, finish: last.finish_reason,
      roles: ms.map(m => m.role).join(','),
    };
  })()`);
  check(report.regen.dupUser === 0,
        `重新生成后没有相邻重复的 user 轮（got=${report.regen.dupUser}）`);
  check(report.regen.adjAssistant === 0,
        `重新生成后没有相邻的 assistant 消息（got=${report.regen.adjAssistant}，roles=${report.regen.roles}）`);
  check(report.regen.userRows === 2, `重新生成不额外插入提问（user 轮=${report.regen.userRows}）`);
  // 重生前是 [.., 旧回答, 续写片段] 两条，重生后应只剩一条全新的回答
  check(report.regen.lastRole === 'assistant' && report.regen.msgCount === beforeRegen - 1,
        `重新生成后以回答收尾（${beforeRegen} → ${report.regen.msgCount} 条）`,
        `roles=${report.regen.roles}`);
  check(report.regen.continued === false && report.regen.finish === 'stop',
        `新回答是完整的一条（continued=${report.regen.continued}, finish=${report.regen.finish}）`);
  // 精确语义断言：重答的是「我刚才问的是什么？」，所以上文应回溯到更早那一轮
  check(report.regen.text.includes('17 × 23'),
        '★ 重新生成时上下文以提问结尾，上文回溯到正确的那一轮',
        report.regen.text.slice(0, 70).replace(/\n/g, ' / '));
  check(!/你(上一|之前)轮?问的是：「我刚才问的是什么？」/.test(report.regen.text),
        '模型不会把本轮提问误当成「上一轮」',
        report.regen.text.slice(0, 70).replace(/\n/g, ' / '));
  await shot('chat-regen');

  /* ---------- 7. 长期记忆面板 ---------- */
  await evalJs(`(() => {
    const m = document.getElementById('chatMemory');
    m.value = '- 用户在做本地大模型监控平台\\n- 偏好中文回答';
    m.dispatchEvent(new Event('input', { bubbles: true }));
  })()`);
  await evalJs(`document.getElementById('btnSaveMemory').click()`);
  await sleep(1500);
  report.memory = await evalJs(`(async () => {
    const r = await fetch('/api/chat/sessions/' + window.CHAT.ST.cur.id).then(x => x.json());
    return {
      saved: r.session.memory || '',
      status: (document.getElementById('chatMemStatus') || {}).textContent || '',
    };
  })()`);
  check(report.memory.saved.includes('偏好中文回答'), '长期记忆已保存到会话');
  check(/2\s*条/.test(report.memory.status), `记忆面板显示条数（${report.memory.status.trim()}）`);

  await evalJs(`window.CHAT.send('chat', '请用一句话总结到这个会话为止的约定。')`);
  await waitFor('!window.CHAT.ST.running', 60000, '记忆轮完成');
  await sleep(1000);
  report.memoryCtx = await evalJs(`(document.getElementById('chatCtx') || {}).innerText || ''`);
  check(/长期记忆\s*\d+\s*字/.test(report.memoryCtx.replace(/\s+/g, ' ')),
        '上下文指示里报告了长期记忆占用',
        report.memoryCtx.replace(/\n/g, ' ').slice(0, 110));

  /* ---------- 8. 编辑重发 / 全屏 ---------- */
  await evalJs(`(() => {
    const b = Array.from(document.querySelectorAll('#chatBox button.act'))
      .find(x => x.dataset.act === 'edit');
    if (b) b.click();
  })()`);
  await sleep(600);
  report.edit = await evalJs(`({
    inputFilled: (document.getElementById('chatInput') || {}).value || '',
    btnText: (document.getElementById('btnSend') || {}).textContent || '',
    status: (document.getElementById('chatStatus') || {}).textContent || '',
  })`);
  check(report.edit.inputFilled.length > 0, '「编辑重发」把原文回填到输入框');
  check(report.edit.btnText.includes('重发'), `发送按钮切换为「重发」（${report.edit.btnText.trim()}）`);
  await evalJs(`document.getElementById('chatInput').dispatchEvent(
    new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))`);
  await sleep(400);
  report.escCancel = await evalJs(`(document.getElementById('btnSend') || {}).textContent || ''`);
  check(report.escCancel.includes('发送') && !report.escCancel.includes('重发'),
        'Esc 可以取消编辑态');

  await evalJs(`document.getElementById('btnChatExpand').click()`);
  await sleep(900);
  report.expand = await evalJs(`({
    expanded: document.getElementById('chatCard').classList.contains('expanded'),
    bodyClass: document.body.classList.contains('chat-expanded'),
    label: (document.getElementById('btnChatExpand') || {}).textContent || '',
    boxH: document.getElementById('chatBox').getBoundingClientRect().height,
  })`);
  check(report.expand.expanded && report.expand.bodyClass, '全屏模式生效（chatCard.expanded）');
  check(report.expand.label.includes('退出'), `全屏按钮文案切换（${report.expand.label.trim()}）`);
  check(report.expand.boxH > 300, `全屏后对话区高度足够（${Math.round(report.expand.boxH)}px）`);
  await shot('chat-expanded');
  await evalJs(`document.getElementById('btnChatExpand').click()`);
  await sleep(600);

  /* ---------- 9. 会话切换与列表同步 ---------- */
  report.sessions = await evalJs(`(async () => {
    const r = await fetch('/api/chat/sessions').then(x => x.json());
    const sel = document.getElementById('chatSessionList');
    return {
      count: r.sessions.length,
      options: sel.options.length,
      selected: sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : '',
      turnsInList: r.sessions.map(s => s.turn_count),
      totalTurns: r.sessions[0] ? r.sessions[0].total_turns : null,
    };
  })()`);
  check(report.sessions.options === report.sessions.count,
        `会话列表与接口一致（${report.sessions.options}/${report.sessions.count}）`);
  check(report.sessions.selected.includes('#'), `列表项带轮数标注（${report.sessions.selected.trim()}）`);

  /* ---------- 10. 窄视口回归 ---------- */
  await c.send('Emulation.setDeviceMetricsOverride', {
    width: 1366, height: 900, deviceScaleFactor: 1, mobile: false,
  });
  await sleep(1500);
  report.narrow = await evalJs(`(() => {
    const box = document.getElementById('chatBox');
    const r = box.getBoundingClientRect();
    return {
      boxW: Math.round(r.width),
      overflowX: document.documentElement.scrollWidth > window.innerWidth + 2,
      docW: document.documentElement.scrollWidth,
      winW: window.innerWidth,
      msgs: document.querySelectorAll('#chatBox .msg').length,
    };
  })()`);
  check(!report.narrow.overflowX,
        `1366px 下无横向溢出（doc=${report.narrow.docW} win=${report.narrow.winW}）`);
  check(report.narrow.boxW > 200, `窄视口下对话区仍可读（${report.narrow.boxW}px）`);
  await shot('chat-narrow');
  await c.send('Emulation.clearDeviceMetricsOverride');
  await sleep(600);

  /* ---------- 11. 关掉「不限制」后，截断必须能指名道姓 ---------- */
  await evalJs(`(() => {
    const un = document.getElementById('chatUnlimited');
    un.checked = false;
    un.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await sleep(400);
  report.unlimitedOff = await evalJs(`({
    disabled: document.getElementById('chatMaxTokens').disabled,
    value: document.getElementById('chatMaxTokens').value,
  })`);
  check(!report.unlimitedOff.disabled && Number(report.unlimitedOff.value) > 0,
        `取消「不限制」后 max_tokens 恢复可编辑并回填默认值（${report.unlimitedOff.value}）`);

  await evalJs(`(() => {
    const mt = document.getElementById('chatMaxTokens');
    mt.value = '120';
    mt.dispatchEvent(new Event('input', { bubbles: true }));
  })()`);
  await evalJs(`window.CHAT.send('chat', '介绍一下 KV Cache 与上下文长度的关系。')`);
  const okCap = await waitFor('!window.CHAT.ST.running', 60000, '限额生成完成');
  check(okCap, '设定 max_tokens=120 的生成完成');
  await sleep(1000);
  report.capped = await evalJs(`(async () => {
    const r = await fetch('/api/chat/sessions/' + window.CHAT.ST.cur.id).then(x => x.json());
    const last = r.messages[r.messages.length - 1] || {};
    const ms = document.querySelectorAll('#chatBox .msg');
    const lastMsg = ms[ms.length - 1] || document.createElement('div');
    return {
      truncated: !!last.truncated, finish: last.finish_reason,
      capInMetrics: (last.metrics || {}).max_tokens,
      badges: Array.from(lastMsg.querySelectorAll('.badge.warn'))
        .map((b) => b.textContent).join(' | '),
      endsMidSentence: /[，、；：]$/.test((last.content || '').trim()),
    };
  })()`);
  check(report.capped.capInMetrics === 120,
        `落库记下本次下发的上限（max_tokens=${report.capped.capInMetrics}）`);
  check(report.capped.truncated && report.capped.finish === 'length',
        `撞上限时如标注终止原因为 length（${report.capped.finish}）`);
  check(/上限\s*120/.test(report.capped.badges),
        `截断标签点名是「面板设的上限 120」而不是含糊的一句截断（${(report.capped.badges || '').trim()}）`);
  check(!report.capped.endsMidSentence, '即使被截断也不停在半个句子上（段边界收尾）');

  // 复原成默认（不限制），免得把状态留给下一次运行
  await evalJs(`(() => {
    const un = document.getElementById('chatUnlimited');
    un.checked = true;
    un.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await sleep(300);
  report.unlimitedRestored = await evalJs(`({
    checked: document.getElementById('chatUnlimited').checked,
    disabled: document.getElementById('chatMaxTokens').disabled,
  })`);
  check(report.unlimitedRestored.checked && report.unlimitedRestored.disabled,
        '重新勾选「不限制」后输入框再次被禁用（状态互斥正确）');

  /* ---------- 收尾 ---------- */
  report.uncaughtExceptions = errors;
  report.consoleErrors = cErrs;
  report.failedRequests = badReqs.filter((u) => !/favicon/.test(u));
  report.assertionFailures = failures;
  process.stdout.write(JSON.stringify(report, null, 2) + '\n');
  c.close();
  process.stderr.write(`\n断言失败：${failures.length}\n`);
  process.exit(failures.length ? 1 : 0);
})().catch((e) => {
  process.stderr.write('VERIFY_FAILED ' + e.message + '\n');
  process.exit(1);
});
