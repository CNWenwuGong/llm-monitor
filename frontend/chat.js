/* =====================================================================
 * 对话测试（多轮 + 记忆）· 前端模块
 *
 * 设计要点
 * --------
 * 1. 正式的多轮对话：每次发送都把**整段会话历史**交给后端，模型因此能
 *    引用之前说过的话；历史与长期记忆都落库，刷新/重启后仍在。
 * 2. 不截断：max_tokens 支持「不限制」；后端透出 finish_reason，
 *    被截断时明确标注并提供「继续生成」，绝不留半句话在那里。
 * 3. 记忆可见：顶部实时显示「载入了多少轮 / 占了多少 token / 省略了几轮」，
 *    长期记忆单独可编辑，也能让模型从当前对话里提炼。
 *
 * 与 app.js 的关系：本文件是独立经典脚本，只用 app.js 暴露在 window 上的
 * toast / logLine / loadModelsInto；其余小工具在本地自带一份，避免耦合。
 * ===================================================================== */
'use strict';

const CHAT = (function () {
  /* ------------------------------ 工具 ------------------------------ */
  const $ = (id) => document.getElementById(id);

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

  const fmt = (v, d = 1) => {
    if (v === null || v === undefined || v === '' || Number.isNaN(Number(v))) return '—';
    return Number(v).toFixed(d);
  };
  const fmtInt = (v) => {
    if (v === null || v === undefined || v === '') return '—';
    return Math.round(Number(v)).toLocaleString('zh-CN');
  };
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const hhmmss = (ts) => {
    if (!ts) return '';
    const d = new Date(String(ts).replace(' ', 'T'));
    return Number.isNaN(d.getTime()) ? String(ts).slice(11, 19) : d.toLocaleTimeString('zh-CN', { hour12: false });
  };

  function toast(msg, kind, ms) {
    if (typeof window.toast === 'function') return window.toast(msg, kind, ms);
    return undefined;
  }
  function logLine(tag, msg, kind) {
    if (typeof window.logLine === 'function') window.logLine(tag, msg, kind);
  }

  /* ------------------------------ 富文本渲染 ------------------------------
   * 自实现的 Markdown 子集渲染器（零外部依赖，离线可用）：
   *   标题 / 粗体 / 斜体 / 删除线 / 高亮 / 行内代码
   *   围栏代码块（语言标签 + 一键复制）
   *   有序、无序、任务列表（支持一级缩进）/ 引用 / 分隔线 / 表格
   *   链接与裸链接自动识别
   *
   * 安全模型：整段文本**先 esc() 转义**，此后插入的标签全部由本函数生成，
   * 模型输出里的 HTML 一律当纯文本 —— 因此不存在注入面。
   * URL 另做 scheme 白名单（仅 http / https / mailto）。
   * -------------------------------------------------------------------- */
  const PH = String.fromCharCode(1);   // 占位符分隔符（正常正文不会出现）

  function mdStore(tag) {
    const items = [];
    const re = new RegExp(PH + tag + '([0-9]+)' + PH, 'g');
    return {
      put(html) { items.push(html); return PH + tag + (items.length - 1) + PH; },
      restore(s) { return s.replace(re, (m, i) => items[+i]); },
    };
  }
  /** 该行是否整行都是块级占位符（代码块 / 表格已被抽走） */
  function isPhLine(s) {
    const t = s.trim();
    return t.length >= 4 && t.charAt(0) === PH && t.charAt(t.length - 1) === PH;
  }

  function safeUrl(u) {
    const s = String(u == null ? '' : u).trim();
    return /^(?:https?:\/\/|mailto:)/i.test(s) ? s : '';
  }

  const RE_UL = /^(\s*)[-*+]\s+(.*)$/;
  const RE_OL = /^(\s*)([0-9]+)[.)]\s+(.*)$/;
  const RE_HR = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
  const RE_HEAD = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
  const RE_QUOTE = /^\s*&gt;\s?/;        // 已转义：行首的 > 变成 &gt;
  const RE_TROW = /^\s*\|.*\|\s*$/;
  const RE_TSEP = /^\s*\|[\s:|-]+\|\s*$/;
  const ATTR = ' target="_blank" rel="noopener noreferrer"';

  function inlineMd(s, st) {
    let t = s;
    t = t.replace(/`([^`\n]+)`/g, (m, c) => st.put('<code>' + c + '</code>'));
    t = t.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (m, txt, u) => {
      const url = safeUrl(u);
      return url ? st.put('<a href="' + url + '"' + ATTR + '>' + txt + '</a>') : txt;
    });
    t = t.replace(/&lt;(https?:\/\/[^\s&]+)&gt;/g,
      (m, u) => st.put('<a href="' + u + '"' + ATTR + '>' + u + '</a>'));
    t = t.replace(/(^|[\s(])(https?:\/\/[^\s<)&]+)/g,
      (m, pre, u) => pre + st.put('<a href="' + u + '"' + ATTR + '>' + u + '</a>'));
    t = t.replace(/\*\*\*([^*\n]+)\*\*\*/g, '<b><i>$1</i></b>');
    t = t.replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
    t = t.replace(/(^|[^*\w])\*([^*\n]+)\*(?=[^*\w]|$)/g, '$1<i>$2</i>');
    t = t.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
    t = t.replace(/==([^=\n]+)==/g, '<mark>$1</mark>');
    return st.restore(t);
  }

  function codeBlockHtml(lang, code) {
    const attr = lang ? ' data-lang="' + lang + '"' : '';
    return '<div class="cb"><div class="cb-h"><span class="cb-lang">' + (lang || 'code') + '</span>'
      + '<button type="button" class="cb-copy" data-act="copycode">复制</button></div>'
      + '<pre' + attr + '><code>' + code + '</code></pre></div>';
  }

  function liBody(text, st) {
    const m = /^\[([ xX])\]\s+([\s\S]*)$/.exec(text);
    if (m) {
      const on = m[1].toLowerCase() === 'x';
      return '<span class="task"><input type="checkbox" disabled' + (on ? ' checked' : '')
        + '><span>' + inlineMd(m[2], st) + '</span></span>';
    }
    return inlineMd(text, st);
  }

  function listHtml(items, ordered, st) {
    const tag = ordered ? 'ol' : 'ul';
    const base = Math.min.apply(null, items.map((x) => x.indent));
    let html = '<' + tag + (ordered && items[0].num > 1 ? ' start="' + items[0].num + '"' : '') + '>';
    let sub = false;
    let open = false;
    items.forEach((it) => {
      if (it.indent > base) {                    // 缩进更深的当作子列表
        if (!sub) { html += '<' + tag + '>'; sub = true; }
        html += '<li>' + liBody(it.text, st) + '</li>';
        return;
      }
      if (sub) { html += '</' + tag + '>'; sub = false; }
      if (open) html += '</li>';
      html += '<li>' + liBody(it.text, st);
      open = true;
    });
    if (sub) html += '</' + tag + '>';
    if (open) html += '</li>';
    return html + '</' + tag + '>';
  }

  function splitRow(l) {
    return l.trim().replace(/^\|/, '').replace(/\|\s*$/, '').split('|').map((c) => c.trim());
  }

  function tableHtml(head, rows, st) {
    const th = head.map((c) => '<th>' + inlineMd(c, st) + '</th>').join('');
    const body = rows.map((r) => {
      const tds = [];
      for (let k = 0; k < head.length; k += 1) {
        tds.push('<td>' + inlineMd(r[k] === undefined ? '' : r[k], st) + '</td>');
      }
      return '<tr>' + tds.join('') + '</tr>';
    }).join('');
    return '<div class="tw"><table><thead><tr>' + th + '</tr></thead>'
      + '<tbody>' + body + '</tbody></table></div>';
  }

  function mdRender(src) {
    if (src === null || src === undefined || src === '') return '';
    const blk = mdStore('B');
    const ist = mdStore('I');
    let t = esc(String(src)).replace(/\r\n?/g, '\n');

    // ① 已闭合的围栏代码块
    t = t.replace(/(^|\n)[ \t]*```[ \t]*([\w+#.-]*)[ \t]*\n([\s\S]*?)\n[ \t]*```[ \t]*(?=\n|$)/g,
      (m, pre, lang, code) => pre + blk.put(codeBlockHtml(lang, code)));
    // ② 流式过程中还没闭合的：按“到文末”处理，避免渲染闪断
    t = t.replace(/(^|\n)[ \t]*```[ \t]*([\w+#.-]*)[ \t]*\n([\s\S]*)$/,
      (m, pre, lang, code) => pre + blk.put(codeBlockHtml(lang, code.replace(/\n+$/, ''))));

    const lines = t.split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const ln = lines[i];

      if (/^\s*$/.test(ln)) { i += 1; continue; }
      if (isPhLine(ln)) { out.push(ln.trim()); i += 1; continue; }

      const mh = RE_HEAD.exec(ln);
      if (mh) {
        const lv = mh[1].length;
        out.push('<h' + lv + '>' + inlineMd(mh[2], ist) + '</h' + lv + '>');
        i += 1; continue;
      }
      if (RE_HR.test(ln)) { out.push('<hr>'); i += 1; continue; }

      if (RE_QUOTE.test(ln)) {
        const q = [];
        while (i < lines.length && RE_QUOTE.test(lines[i])) {
          q.push(lines[i].replace(RE_QUOTE, '')); i += 1;
        }
        out.push('<blockquote>' + q.map((x) => inlineMd(x, ist)).join('<br>') + '</blockquote>');
        continue;
      }

      if (RE_UL.test(ln) || RE_OL.test(ln)) {
        const ordered = !RE_UL.test(ln);
        const items = [];
        while (i < lines.length) {
          const mo = RE_OL.exec(lines[i]);
          if (mo) { items.push({ indent: mo[1].length, num: Number(mo[2]), text: mo[3] }); i += 1; continue; }
          const mu = RE_UL.exec(lines[i]);
          if (mu) { items.push({ indent: mu[1].length, num: 1, text: mu[2] }); i += 1; continue; }
          break;
        }
        out.push(listHtml(items, ordered, ist));
        continue;
      }

      if (RE_TROW.test(ln) && i + 1 < lines.length && RE_TSEP.test(lines[i + 1])) {
        const head = splitRow(ln);
        i += 2;
        const rows = [];
        while (i < lines.length && RE_TROW.test(lines[i])) { rows.push(splitRow(lines[i])); i += 1; }
        out.push(tableHtml(head, rows, ist));
        continue;
      }

      // 段落：一路吃到下一处块级起始或空行
      const para = [ln];
      i += 1;
      while (i < lines.length && lines[i].trim() !== ''
             && !RE_HEAD.test(lines[i]) && !RE_HR.test(lines[i]) && !RE_QUOTE.test(lines[i])
             && !RE_UL.test(lines[i]) && !RE_OL.test(lines[i]) && !RE_TROW.test(lines[i])
             && !isPhLine(lines[i])) {
        para.push(lines[i]); i += 1;
      }
      out.push('<p>' + para.map((x) => inlineMd(x, ist)).join('<br>') + '</p>');
    }
    return blk.restore(out.join('\n'));
  }

  /* 流式渲染节流：每 60ms 最多重排一次。长回答时若每个 delta 都全量重解析
     会明显掉帧；收尾时再用 paintTextNow 强制落到最终内容，保证不丢字。 */
  let richTimer = 0;
  let richJob = null;
  function paintText(node, text) {
    if (!node) return;
    richJob = { node, text };
    if (richTimer) return;
    richTimer = setTimeout(() => {
      richTimer = 0;
      const job = richJob;
      richJob = null;
      if (job && job.node && job.node.isConnected) job.node.innerHTML = mdRender(job.text);
    }, 60);
  }
  function paintTextNow(node, text) {
    if (richTimer) { clearTimeout(richTimer); richTimer = 0; }
    richJob = null;
    if (node) node.innerHTML = mdRender(text);
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
    return res.json();
  }

  /* ------------------------------ 状态 ------------------------------ */
  const ST = {
    sessions: [],
    cur: null,
    messages: [],
    running: false,
    ctrl: null,
    buf: '',
    editFrom: null,
    loading: false,
    defaults: { ctx_budget: 8192, keep_turns: 6, max_tokens: 1024 },
    lastCtx: null,
  };

  const LS_KEY = 'llm-monitor.chat.session';
  // 「不限制回答长度」的用户偏好：默认开。真实的对话不该被一个没人指定的
  // 1024 上限砍断——踩过这个坑：默认 1024 直接导致 llama.cpp 在 1024 token
  // 处返回 stop_type=limit，看起来就像"刚输出一点就被截断"。
  const LS_UNLIMITED = 'llm-monitor.chat.unlimited';

  /* ------------------------------ 参数面板同步 ------------------------------ */
  function syncPanel() {
    const s = ST.cur;
    if ($('chatSystem')) $('chatSystem').value = s ? (s.system || '') : '';
    if ($('chatMemory')) $('chatMemory').value = s ? (s.memory || '') : '';
    if ($('chatCtxBudget')) $('chatCtxBudget').value = (s && s.ctx_budget) ? s.ctx_budget : '';
    if ($('chatMemStatus')) {
      $('chatMemStatus').textContent = s
        ? `${(s.memory || '').trim() ? `${(s.memory || '').split('\n').filter(Boolean).length} 条` : '空'}`
        : '';
    }
    if (!s) return;
    // 会话里记着的连接/模型回填到参数面板（模型列表异步拉取）
    if ($('chatConn') && s.conn_id && String($('chatConn').value) !== String(s.conn_id)) {
      $('chatConn').value = String(s.conn_id);
      if (typeof window.loadModelsInto === 'function') {
        window.loadModelsInto('chatModel', s.conn_id);
      }
    }
    setTimeout(() => {
      if (!s.model || !$('chatModel')) return;
      const opt = Array.from($('chatModel').options).find((o) => o.value === s.model);
      if (opt) $('chatModel').value = s.model;
    }, 400);
  }

  /* ------------------------------ 会话列表 ------------------------------ */
  function renderSessionList() {
    const sel = $('chatSessionList');
    if (!sel) return;
    if (!ST.sessions.length) {
      sel.innerHTML = '<option value="">（还没有会话，发送时自动新建）</option>';
      return;
    }
    sel.innerHTML = ST.sessions.map((s) => {
      const n = s.turn_count === null || s.turn_count === undefined ? '' : ` · ${s.turn_count} 轮`;
      return `<option value="${s.id}" ${ST.cur && ST.cur.id === s.id ? 'selected' : ''}>` +
        `#${s.id} ${esc(s.title)}${n}</option>`;
    }).join('');
  }

  /* ------------------------------ 参数面板 ------------------------------ */
  /** max_tokens 与「不限制」开关互斥：勾选时不发送该字段，输入框禁用并给出占位提示。 */
  function syncMaxTokens() {
    const box = $('chatUnlimited');
    const e = $('chatMaxTokens');
    if (!box || !e) return;
    if (box.checked) {
      // 记住用户之前填过的上限，取消勾选时还能回填
      if (e.value && Number(e.value) > 0) e.dataset.prev = e.value;
      e.value = '';
      e.disabled = true;
      e.placeholder = '不限制（后端不接收 max_tokens）';
    } else {
      e.disabled = false;
      e.placeholder = '例如 1024';
      if (!e.value || Number(e.value) <= 0) {
        e.value = (e.dataset.prev && Number(e.dataset.prev) > 0)
          ? e.dataset.prev : String(ST.defaults.max_tokens || 1024);
      }
    }
    updateChipLen();
  }

  /* ------------------------------ 输入区工具栏 ------------------------------ */
  /** 输入框随内容自增高（上限 200px，再往上滚动） */
  function autoGrow() {
    const el = $('chatInput');
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }

  /** 「回答长度」胶囊：与参数面板的 chatUnlimited / chatMaxTokens 共用同一份真相 */
  function updateChipLen() {
    const box = $('chatUnlimited');
    if (!box) return;
    const unlimited = !!box.checked;
    const v = Number($('chatMaxTokens') ? $('chatMaxTokens').value : 0) || 0;
    const ic = $('chipLenIc');
    const tx = $('chipLenTx');
    if (ic) ic.textContent = unlimited ? '∞' : '↕';
    if (tx) {
      tx.textContent = unlimited
        ? '回答长度：不限制'
        : `回答长度：${fmtInt(v > 0 ? v : ST.defaults.max_tokens)} tok`;
    }
    const pop = $('chipLenPop');
    if (pop) {
      Array.from(pop.querySelectorAll('.cp-item')).forEach((b) => {
        const n = Number(b.dataset.len);
        b.classList.toggle('on', unlimited ? n === 0 : (n > 0 && n === v));
      });
    }
  }

  function closeChipLen() {
    const pop = $('chipLenPop');
    const btn = $('btnChipLen');
    if (pop) pop.classList.remove('open');
    if (btn) btn.setAttribute('aria-expanded', 'false');
  }

  function toggleChipLen() {
    const pop = $('chipLenPop');
    if (!pop) return;
    const open = !pop.classList.contains('open');
    pop.classList.toggle('open', open);
    if ($('btnChipLen')) $('btnChipLen').setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) updateChipLen();
  }

  function applyChipLen(raw) {
    const v = String(raw);
    const un = $('chatUnlimited');
    const mt = $('chatMaxTokens');
    closeChipLen();
    if (v === 'custom') {
      if (un) { un.checked = false; localStorage.setItem(LS_UNLIMITED, '0'); }
      syncMaxTokens();
      if (mt) {
        mt.focus();
        if (mt.select) mt.select();
        const card = mt.closest ? mt.closest('.card') : null;
        if (card && card.scrollIntoView) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
      return;
    }
    const n = Number(v);
    if (n > 0) {
      if (un) un.checked = false;
      localStorage.setItem(LS_UNLIMITED, '0');
      if (mt) { mt.dataset.touched = '1'; mt.dataset.prev = String(n); mt.value = String(n); }
    } else {
      if (un) un.checked = true;
      localStorage.setItem(LS_UNLIMITED, '1');
    }
    syncMaxTokens();
  }

  async function loadSessions(preferId) {
    const r = await api('GET', '/api/chat/sessions');
    ST.sessions = r.sessions || [];
    if (r.defaults) {
      ST.defaults = Object.assign({}, ST.defaults, r.defaults);
      if ($('chatMaxTokens') && !$('chatMaxTokens').dataset.prev) {
        // 只作为「取消不限制」时的回填值，不直接写进输入框
        $('chatMaxTokens').dataset.prev = String(ST.defaults.max_tokens || 1024);
      }
      syncMaxTokens();
    }
    let id = preferId;
    if (!id && ST.cur) id = ST.cur.id;
    if (!id) {
      const saved = Number(localStorage.getItem(LS_KEY) || 0);
      id = saved || (ST.sessions[0] ? ST.sessions[0].id : 0);
    }
    if (id && ST.sessions.some((s) => s.id === Number(id))) {
      await openSession(Number(id));
    } else if (ST.sessions.length) {
      await openSession(ST.sessions[0].id);
    } else {
      ST.cur = null;
      ST.messages = [];
      renderSessionList();
      renderMessages();
      applyContext(null);
    }
  }

  async function openSession(id) {
    ST.loading = true;
    try {
      const r = await api('GET', `/api/chat/sessions/${id}`);
      ST.cur = r.session;
      ST.messages = r.messages || [];
      localStorage.setItem(LS_KEY, String(id));
      renderSessionList();
      renderMessages();
      applyContext(r.context);
      syncPanel();
    } finally {
      ST.loading = false;
    }
  }

  async function newSession() {
    if (ST.running) return toast('当前还在生成，先停止再新建会话', 'warn');
    const connId = $('chatConn') && $('chatConn').value ? Number($('chatConn').value) : null;
    const model = $('chatModel') ? $('chatModel').value : '';
    const r = await api('POST', '/api/chat/sessions', {
      conn_id: connId, model, system: $('chatSystem') ? $('chatSystem').value.trim() : '',
    });
    toast(`已新建会话 #${r.session.id}`, 'ok');
    ST.cur = r.session;
    ST.messages = [];
    localStorage.setItem(LS_KEY, String(r.session.id));
    await loadSessions(r.session.id);
    if ($('chatInput')) $('chatInput').focus();
  }

  async function renameSession() {
    if (!ST.cur) return toast('还没有会话可以重命名', 'warn');
    const name = window.prompt('会话名称', ST.cur.title || '');
    if (name === null) return;
    const r = await api('PATCH', `/api/chat/sessions/${ST.cur.id}`, { title: name.trim() || '新会话' });
    ST.cur = r.session;
    await loadSessions(r.session.id);
  }

  async function deleteSession() {
    if (!ST.cur) return toast('还没有会话可以删除', 'warn');
    if (!window.confirm(`删除会话「${ST.cur.title}」及其全部消息？此操作不可恢复。`)) return;
    await api('DELETE', `/api/chat/sessions/${ST.cur.id}`);
    localStorage.removeItem(LS_KEY);
    ST.cur = null;
    ST.messages = [];
    toast('会话已删除', 'ok');
    await loadSessions(0);
  }

  async function clearMessages() {
    if (!ST.cur) {
      ST.messages = [];
      renderMessages();
      setMetrics(null);
      return;
    }
    if (!window.confirm('清空当前会话的全部对话内容（会话本身与长期记忆保留）？')) return;
    const r = await api('DELETE', `/api/chat/sessions/${ST.cur.id}/messages`);
    ST.messages = [];
    if (r.session) ST.cur = r.session;
    applyContext(null);
    await loadSessions(ST.cur.id);
    logLine('CHAT', `清空会话 #${ST.cur.id} 的 ${r.deleted} 条消息`, 'warn');
  }

  /* ------------------------------ 消息渲染 ------------------------------ */
  /** 「谁把输出截断了」——本次下发的上限（<=0 / 缺省 = 不限制）。
   *
   *  不分清就会把「服务端自己限制了长度」错标成「我们的 max_tokens 截断」，
   *  用户照着面板的 1024 去查会一无所获（llama.cpp 的 stop_type=limit 也可能来自
   *  服务端启动参数 -n/--n-predict 或上下文窗口耗尽）。 */
  function truncNote(cap) {
    if (cap === null || cap === undefined) {
      return { badge: '输出被截断（终止原因 length）',
               hint: '输出在长度上限处停止，可点「继续生成」补完' };
    }
    if (Number(cap) > 0) {
      return {
        badge: `已 max_tokens 截断（上限 ${fmtInt(cap)}）`,
        hint: `回答达到面板设置的 ${fmtInt(cap)} token 上限；可点「继续生成」补完，`
            + '或勾选「不限制回答长度」让模型自然收尾',
      };
    }
    return {
      badge: '被服务端长度上限截断（本面板未设 max_tokens）',
      hint: '本面板没有下发长度上限，是服务端自己停的：检查 llama-server 的 '
          + '-n/--n-predict、Ollama 的 num_predict，或上下文窗口是否已被占满',
    };
  }

  function metaLine(m) {
    const k = m.metrics || {};
    const bits = [];
    if (k.ttft_ms !== undefined && k.ttft_ms !== null) bits.push(`TTFT ${fmt(k.ttft_ms, 1)} ms`);
    if (k.e2e_ms !== undefined && k.e2e_ms !== null) bits.push(`E2E ${fmt(k.e2e_ms, 1)} ms`);
    if (k.tps !== undefined && k.tps !== null) bits.push(`${fmt(k.tps, 2)} tok/s`);
    if (m.completion_tokens !== null && m.completion_tokens !== undefined) {
      bits.push(`${fmtInt(m.completion_tokens)} tokens`);
    }
    if (k.finish_reason) bits.push(`终止：${esc(k.finish_reason)}`);
    return bits.length ? bits.join(' · ') : '';
  }

  function messageHtml(m) {
    const isUser = m.role === 'user';
    const k = m.metrics || {};
    const cls = isUser ? 'user' : (m.continued ? 'ai cont' : 'ai');
    const who = isUser ? 'YOU' : (m.continued ? 'AI（续）' : 'AI');
    const time = hhmmss(m.created_at);
    const tk = isUser ? '' : metaLine(m);
    let badges = '';
    if (m.truncated) {
      badges += `<span class="badge warn">${esc(truncNote(k.max_tokens).badge)}</span>`;
    }
    if (m.created_at && time) badges += `<span class="badge time">${time}</span>`;
    const acts = isUser
      ? `<button class="act" data-act="edit" data-id="${m.id}">编辑重发</button>
         <button class="act" data-act="copy" data-id="${m.id}">复制</button>`
      : `<button class="act" data-act="copy" data-id="${m.id}">复制</button>
         <button class="act" data-act="regen" data-id="${m.id}">重新生成</button>
         <button class="act" data-act="cont" data-id="${m.id}">继续生成</button>
         <button class="act" data-act="cut" data-id="${m.id}">删除此后</button>`;
    return `<div class="msg ${cls}" data-id="${m.id}">
      <div class="who">${who}</div>
      <div class="body">
        <div class="text">${mdRender(m.content || '')}</div>
        ${tk ? `<div class="meta-line">${tk}</div>` : ''}
        <div class="acts">${badges}${acts}</div>
      </div>
    </div>`;
  }

  function renderMessages() {
    const box = $('chatBox');
    if (!box) return;
    if (!ST.messages.length) {
      box.innerHTML = '<div class="empty">还没有对话，输入一条 Prompt 开始测试。'
        + '历史会自动保存到当前会话，下一轮提问时作为「记忆」一起发给模型。</div>';
      return;
    }
    box.innerHTML = ST.messages.map(messageHtml).join('');
    box.scrollTop = box.scrollHeight;
  }

  function appendLocal(role, html, continued) {
    const box = $('chatBox');
    const empty = box.querySelector('.empty');
    if (empty) empty.remove();
    const div = document.createElement('div');
    div.className = continued ? `msg ${role} cont` : `msg ${role}`;
    div.innerHTML = `<div class="who">${role === 'user' ? 'YOU' : (continued ? 'AI（续）' : 'AI')}</div>
      <div class="body"><div class="text">${html}</div></div>`;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
    return div;
  }

  /* ------------------------------ 上下文（记忆）指示 ------------------------------ */
  function applyContext(info) {
    const box = $('chatCtx');
    if (!box) return;
    ST.lastCtx = info;
    if (!info) {
      box.innerHTML = '记忆：<span class="muted">当前会话还没有上下文（第一条提问）</span>';
      return;
    }
    const pct = clamp((info.turn_tokens || 0) / Math.max(1, info.budget || 1) * 100, 0, 100);
    const cls = pct > 90 ? 'red' : (pct > 70 ? 'yellow' : 'green');
    const bits = [
      `载入 <b>${info.turns_loaded}/${info.turns_total}</b> 轮`,
      `上下文 ≈ <b>${fmtInt(info.turn_tokens)}</b> / ${fmtInt(info.budget)} tok`,
    ];
    if (info.reserve) bits.push(`预留输出 ${fmtInt(info.reserve)} tok`);
    bits.push(info.memory_used ? `长期记忆 <b>${fmtInt(info.memory_chars)}</b> 字` : '未启用长期记忆');
    if (info.dropped_turns) bits.push(`<span class="warn-t">已省略 ${info.dropped_turns} 轮</span>`);
    box.innerHTML = `记忆：${bits.join(' · ')}
      <span class="ctx-bar"><i class="${cls}" style="width:${pct.toFixed(1)}%"></i></span>`;
  }

  function setMetrics(m) {
    const ids = ['ttft', 'e2e', 'tps', 'pt', 'ct', 'gent'];
    if (!m) {
      ids.forEach((k) => { if ($(`m-${k}`)) $(`m-${k}`).textContent = '—'; });
      if ($('m-utilBar')) $('m-utilBar').style.width = '0%';
      if ($('m-utilTxt')) $('m-utilTxt').textContent = '—';
      return;
    }
    if ($('m-ttft')) $('m-ttft').textContent = `${fmt(m.ttft_ms, 1)} ms`;
    if ($('m-e2e')) $('m-e2e').textContent = `${fmt(m.e2e_ms, 1)} ms`;
    if ($('m-tps')) $('m-tps').textContent = `${fmt(m.tps, 2)} tok/s`;
    if ($('m-pt')) $('m-pt').textContent = fmtInt(m.prompt_tokens);
    if ($('m-ct')) $('m-ct').textContent = fmtInt(m.completion_tokens);
    if ($('m-gent')) $('m-gent').textContent = `${fmt(m.gen_ms, 1)} ms`;
    const util = m.token_utilization;
    if ($('m-utilBar')) {
      $('m-utilBar').style.width = util === null || util === undefined
        ? '0%' : `${clamp(util * 100, 0, 100)}%`;
      $('m-utilBar').className = 'bar-inner '
        + (util === null || util === undefined ? 'green' : (util > 0.95 ? 'red' : util > 0.7 ? 'green' : 'yellow'));
    }
    if ($('m-utilTxt')) {
      $('m-utilTxt').textContent = util === null || util === undefined
        ? '不限制输出长度' : `${fmt(util * 100, 1)}%`;
    }
  }

  /* ------------------------------ 发送（SSE） ------------------------------ */
  function currentParams() {
    const unlimited = $('chatUnlimited') && $('chatUnlimited').checked;
    const mt = unlimited ? 0 : Number($('chatMaxTokens') ? $('chatMaxTokens').value : 1024);
    return {
      conn_id: Number($('chatConn') ? $('chatConn').value : 0),
      model: $('chatModel') ? $('chatModel').value || '' : '',
      system: $('chatSystem') ? $('chatSystem').value.trim() : '',
      temperature: Number($('chatTemp') ? $('chatTemp').value : 0.7),
      top_p: Number($('chatTopP') ? $('chatTopP').value : 0.9),
      max_tokens: Number.isFinite(mt) ? mt : 1024,
      num_ctx: $('chatNumCtx') && $('chatNumCtx').value ? Number($('chatNumCtx').value) : null,
      ctx_budget: $('chatCtxBudget') && $('chatCtxBudget').value
        ? Number($('chatCtxBudget').value) : null,
      use_memory: $('chatUseMemory') ? $('chatUseMemory').checked : true,
      stream: true,
      save: $('chatSave') ? $('chatSave').checked : true,
    };
  }

  async function ensureSession() {
    if (ST.cur) return ST.cur;
    const p = currentParams();
    if (!p.conn_id) throw new Error('请先选择连接档案');
    const r = await api('POST', '/api/chat/sessions', {
      conn_id: p.conn_id, model: p.model, system: p.system,
    });
    ST.cur = r.session;
    ST.messages = [];
    localStorage.setItem(LS_KEY, String(r.session.id));
    renderSessionList();
    renderMessages();
    return ST.cur;
  }

  async function send(mode, text, opts) {
    const o = opts || {};
    if (ST.running) return toast('上一个请求还在进行中', 'warn');
    const params = currentParams();
    if (!params.conn_id) return toast('请先选择连接档案', 'warn');
    const prompt = (text === undefined ? ($('chatInput') ? $('chatInput').value.trim() : '') : String(text));
    if (mode !== 'continue' && !prompt) return toast('请输入 Prompt', 'warn');

    let session;
    try {
      session = await ensureSession();
    } catch (e) {
      return toast(e.message, 'warn');
    }

    // 编辑重发：先把它之后的消息删掉（含被编辑的那条），再当新提问发出去
    if (o.editFrom) {
      await api('DELETE', `/api/chat/sessions/${session.id}/messages?from_id=${o.editFrom}`);
      ST.messages = ST.messages.filter((m) => m.id < o.editFrom);
      renderMessages();
    }
    if (o.regenerateAfter) {
      await api('DELETE', `/api/chat/sessions/${session.id}/messages?from_id=${o.regenerateAfter}`);
      ST.messages = ST.messages.filter((m) => m.id < o.regenerateAfter);
      renderMessages();
    }

    const payload = Object.assign({}, params, {
      prompt: mode === 'continue' ? '' : prompt,
      session_id: session.id,
      mode,
      echo_user: mode === 'chat',
    });
    // 面板上的 System Prompt 为空时不下发，由后端取会话级的那个
    if (!payload.system) delete payload.system;

    if (mode === 'chat') appendLocal('user', `<p>${esc(prompt)}</p>`);
    if ($('chatInput') && mode === 'chat') { $('chatInput').value = ''; autoGrow(); }
    const aiNode = appendLocal('ai', '<p class="muted">正在生成…</p>', mode === 'continue');
    const textNode = aiNode.querySelector('.text');

    ST.running = true;
    ST.buf = '';
    if ($('btnSend')) $('btnSend').disabled = true;
    if ($('btnStopChat')) $('btnStopChat').disabled = false;
    if ($('chatStatus')) {
      $('chatStatus').textContent = mode === 'continue' ? '继续生成中…' : '流式接收中…';
    }
    setMetrics(null);
    const t0 = performance.now();
    let firstToken = 0;
    let chars = 0;
    let done = false;
    let truncated = false;
    let lastMaxTokens = payload.max_tokens;   // 本次下发的上限（0 = 不限制）

    const ctrl = new AbortController();
    ST.ctrl = ctrl;
    try {
      const res = await fetch('/api/test/single', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload), signal: ctrl.signal,
      });
      if (!res.ok) {
        let m = `HTTP ${res.status}`;
        try { m = (await res.json()).detail || m; } catch (e) { /* ignore */ }
        throw new Error(m);
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buf += dec.decode(chunk.value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const raw = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const line = raw.startsWith('data:') ? raw.slice(5).trim() : raw.trim();
          if (!line) continue;
          let evt;
          try { evt = JSON.parse(line); } catch (e) { continue; }
          if (evt.event === 'start') {
            if ($('chatRunId')) {
              $('chatRunId').textContent = evt.run_id ? `Run #${evt.run_id}` : '未保存';
            }
            if (evt.context) applyContext(evt.context);
          } else if (evt.event === 'delta') {
            if (!firstToken) firstToken = performance.now() - t0;
            ST.buf += evt.text;
            chars += evt.text.length;
            paintText(textNode, ST.buf);
            const box = $('chatBox');
            if (box) box.scrollTop = box.scrollHeight;
            if ($('chatStatus')) {
              $('chatStatus').innerHTML =
                `<span class="spin"></span> 已接收 ${chars} 字符 · TTFT ${fmt(firstToken, 0)} ms`;
            }
          } else if (evt.event === 'metrics') {
            const m = evt.data;
            setMetrics(m);
            truncated = !!m.truncated;
            if (m.max_tokens_cfg !== undefined && m.max_tokens_cfg !== null) {
              lastMaxTokens = m.max_tokens_cfg;
            }
            const bits = [`TTFT ${fmt(m.ttft_ms, 1)} ms`, `E2E ${fmt(m.e2e_ms, 1)} ms`,
              `${fmt(m.tps, 2)} tok/s`, `${fmtInt(m.completion_tokens)} tokens`];
            if (m.finish_reason) bits.push(`终止：${m.finish_reason}`);
            aiNode.querySelector('.body').insertAdjacentHTML('beforeend',
              `<div class="meta-line">${bits.join(' · ')}</div>`);
            if (truncated) {
              aiNode.querySelector('.body').insertAdjacentHTML('beforeend',
                `<div class="acts"><span class="badge warn">${esc(truncNote(m.max_tokens_cfg).hint)}</span></div>`);
            }
            if (m.session) ST.cur = m.session;
            if ($('chatCtx') && m.session) {
              // 会话统计变了，顺手刷新列表里的轮数
              const idxS = ST.sessions.findIndex((s) => s.id === m.session.id);
              if (idxS >= 0) ST.sessions[idxS] = Object.assign({}, ST.sessions[idxS], m.session);
              renderSessionList();
            }
            logLine('CHAT', `#${session.id} ${mode} 模型 ${payload.model || '默认'} `
              + `TTFT=${fmt(m.ttft_ms, 1)}ms TPS=${fmt(m.tps, 2)} `
              + `tokens=${fmtInt(m.completion_tokens)}${truncated ? ' [截断]' : ''}`, 'ok');
          } else if (evt.event === 'done') {
            done = true;
            paintTextNow(textNode, ST.buf);   // 收尾：强制落到最终内容，避免节流丢字
            if ($('chatStatus')) {
              $('chatStatus').textContent = evt.status === 'ok'
                ? (truncated ? '完成（已截断，可继续生成）' : '完成')
                : `结束：${evt.status}`;
            }
          } else if (evt.event === 'error') {
            paintTextNow(textNode, '');
            textNode.innerHTML = `<span style="color:var(--red)">请求出错：${esc(evt.message)}</span>`;
            logLine('CHAT', `请求出错: ${evt.message}`, 'err');
          }
        }
      }
    } catch (e) {
      if (e.name === 'AbortError') {
        paintTextNow(textNode, ST.buf);
        textNode.insertAdjacentHTML('beforeend', '<p class="muted">（已手动停止）</p>');
        if ($('chatStatus')) $('chatStatus').textContent = '已停止';
        logLine('CHAT', '用户手动停止流式请求', 'warn');
      } else {
        paintTextNow(textNode, '');
        textNode.innerHTML = `<span style="color:var(--red)">请求失败：${esc(e.message)}</span>`;
        if ($('chatStatus')) $('chatStatus').textContent = '失败';
        logLine('CHAT', `请求失败: ${e.message}`, 'err');
      }
    } finally {
      ST.running = false;
      ST.ctrl = null;
      if ($('btnSend')) $('btnSend').disabled = false;
      if ($('btnStopChat')) $('btnStopChat').disabled = true;
      setEditMode(null);
      // 以服务端为准重载一次：拿到真实的消息 id，后续才能「编辑/重新生成」
      try {
        const keep = ST.cur ? ST.cur.id : 0;
        if (keep) {
          await loadSessions(keep);
        }
      } catch (e) {
        logLine('CHAT', `刷新会话失败: ${e.message}`, 'warn');
      }
      if (done && truncated) toast(truncNote(lastMaxTokens).hint, 'warn', 6000);
    }
  }

  function setEditMode(fromId) {
    ST.editFrom = fromId;
    const btn = $('btnSend');
    if (!btn) return;
    // 发送按钮现在是圆形图标：文案放在 sr-only 里，既可读屏也让回归脚本
    // 依旧能通过 textContent 判断「发送 / 重发」两种状态
    const label = btn.querySelector('.sr-only');
    if (label) label.textContent = fromId ? '重发' : '发送';
    btn.classList.toggle('resend', !!fromId);
    btn.title = fromId ? '重发：将删除该提问及其之后的所有消息' : '发送（Enter）';
    if (!fromId && $('chatStatus') && $('chatStatus').textContent.indexOf('编辑中') === 0) {
      $('chatStatus').textContent = '';
    }
  }

  async function onBoxAction(ev) {
    const btn = ev.target.closest ? ev.target.closest('[data-act]') : null;
    if (!btn) return;
    const act = btn.dataset.act;

    // 代码块右上角的「复制」：直接取同级 <code> 的纯文本，无需回查消息
    if (act === 'copycode') {
      const shell = btn.closest('.cb');
      const codeEl = shell ? shell.querySelector('code') : null;
      if (!codeEl) return;
      try {
        await navigator.clipboard.writeText(codeEl.textContent || '');
        if (btn.dataset.busy !== '1') {
          btn.dataset.busy = '1';
          const old = btn.textContent;
          btn.textContent = '已复制';
          setTimeout(() => { btn.textContent = old; delete btn.dataset.busy; }, 1200);
        }
        toast('代码已复制到剪贴板', 'ok');
      } catch (e) {
        toast(`复制失败：${e.message}`, 'err');
      }
      return;
    }

    const id = Number(btn.dataset.id);
    const msg = ST.messages.find((m) => m.id === id);
    if (!msg) return;

    if (act === 'copy') {
      try {
        await navigator.clipboard.writeText(msg.content || '');
        toast('已复制到剪贴板', 'ok');
      } catch (e) {
        toast('复制失败，请手动选择文本', 'err');
      }
      return;
    }
    if (act === 'edit') {
      if ($('chatInput')) $('chatInput').value = msg.content || '';
      autoGrow();
      setEditMode(msg.id);
      if ($('chatStatus')) $('chatStatus').textContent = '编辑中…发送将重发并覆盖其后的对话（Esc 取消）';
      if ($('chatInput')) $('chatInput').focus();
      return;
    }
    if (act === 'cont') {
      return send('continue');
    }
    if (act === 'cut') {
      if (!window.confirm('删除这条消息及其之后的全部消息？')) return;
      await api('DELETE', `/api/chat/sessions/${ST.cur.id}/messages?from_id=${id}`);
      await loadSessions(ST.cur.id);
      return;
    }
    if (act === 'regen') {
      let idx = ST.messages.findIndex((m) => m.id === id);
      // 点的是「续写」出来的片段时，回溯到这一组回答里最初的那条，整体重生。
      // 否则只删掉续写段会让库里留下两条相邻的 assistant 消息。
      let start = idx;
      while (start > 0 && ST.messages[start].continued) start -= 1;
      idx = start;
      let userMsg = null;
      for (let i = idx - 1; i >= 0; i -= 1) {
        if (ST.messages[i].role === 'user') { userMsg = ST.messages[i]; break; }
      }
      if (!userMsg) return toast('找不到对应的提问，无法重新生成', 'warn');
      return send('regenerate', userMsg.content,
        { regenerateAfter: ST.messages[idx].id });
    }
    return undefined;
  }

  /* ------------------------------ 长期记忆 ------------------------------ */
  async function saveMemory() {
    if (!ST.cur) return toast('还没有会话', 'warn');
    const txt = $('chatMemory') ? $('chatMemory').value : '';
    const r = await api('PATCH', `/api/chat/sessions/${ST.cur.id}`, { memory: txt });
    ST.cur = r.session;
    syncPanel();
    applyContext(ST.lastCtx);
    toast('长期记忆已保存', 'ok');
    logLine('CHAT', `会话 #${ST.cur.id} 长期记忆已更新（${txt.trim().length} 字）`, 'ok');
  }

  async function distillMemory() {
    if (!ST.cur) return toast('还没有会话', 'warn');
    if (ST.running) return toast('生成中，请稍后再试', 'warn');
    const btn = $('btnDistillMemory');
    if (btn) { btn.disabled = true; btn.textContent = '提炼中…'; }
    try {
      const r = await api('POST', `/api/chat/sessions/${ST.cur.id}/memory/distill`, {});
      if (r.session) ST.cur = r.session;
      if ($('chatMemory')) $('chatMemory').value = r.memory || '';
      syncPanel();
      toast(r.reason || (r.ok ? '已提炼记忆' : '提炼失败'), r.ok ? (r.added && r.added.length ? 'ok' : 'info') : 'warn');
      if (r.added && r.added.length) {
        logLine('CHAT', `从 ${r.messages_used} 条消息中提炼出 ${r.added.length} 条长期记忆`, 'ok');
      }
    } catch (e) {
      toast(`提炼失败：${e.message}`, 'err');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '从对话提炼'; }
    }
  }

  async function clearMemory() {
    if (!ST.cur) return;
    if (!window.confirm('清空该会话的长期记忆？')) return;
    if ($('chatMemory')) $('chatMemory').value = '';
    await saveMemory();
  }

  /* ------------------------------ 全屏 ------------------------------ */
  function toggleExpand() {
    const card = $('chatCard');
    if (!card) return;
    const on = card.classList.toggle('expanded');
    document.body.classList.toggle('chat-expanded', on);
    if ($('btnChatExpand')) $('btnChatExpand').textContent = on ? '退出全屏' : '全屏';
    const box = $('chatBox');
    if (box) setTimeout(() => { box.scrollTop = box.scrollHeight; }, 60);
  }

  /* ------------------------------ 初始化 ------------------------------ */
  function init() {
    const bind = (id, ev, fn) => { const e = $(id); if (e) e.addEventListener(ev, fn); };

    bind('btnSend', 'click', () => send(ST.editFrom ? 'chat' : 'chat', undefined,
      ST.editFrom ? { editFrom: ST.editFrom } : undefined));
    bind('btnStopChat', 'click', () => { if (ST.ctrl) ST.ctrl.abort(); });
    bind('btnClearChat', 'click', clearMessages);
    bind('btnComposerNew', 'click', () => { newSession().catch((e) => toast(e.message, 'err')); });
    bind('btnChipLen', 'click', (e) => { e.stopPropagation(); toggleChipLen(); });
    const chipPop = $('chipLenPop');
    if (chipPop) {
      chipPop.addEventListener('click', (e) => {
        const item = e.target.closest ? e.target.closest('.cp-item') : null;
        if (item) applyChipLen(item.dataset.len);
      });
    }
    // 点空白处收起胶囊菜单
    document.addEventListener('click', (e) => {
      const t = e.target;
      if (t && t.closest && (t.closest('#chipLenPop') || t.closest('#btnChipLen'))) return;
      closeChipLen();
    });
    bind('btnNewChat', 'click', () => { newSession().catch((e) => toast(e.message, 'err')); });
    bind('btnRenameChat', 'click', () => { renameSession().catch((e) => toast(e.message, 'err')); });
    bind('btnDeleteChat', 'click', () => { deleteSession().catch((e) => toast(e.message, 'err')); });
    bind('btnToggleMem', 'click', () => {
      const c = $('chatMemCard');
      if (c) c.style.display = c.style.display === 'none' ? '' : 'none';
    });
    bind('btnSaveMemory', 'click', () => { saveMemory().catch((e) => toast(e.message, 'err')); });
    bind('btnDistillMemory', 'click', distillMemory);
    bind('btnClearMemory', 'click', clearMemory);
    bind('btnChatExpand', 'click', toggleExpand);
    bind('chatSessionList', 'change', (e) => {
      if (ST.running) { toast('生成中，稍后再切换会话', 'warn'); renderSessionList(); return; }
      openSession(Number(e.target.value)).catch((err) => toast(err.message, 'err'));
    });
    bind('chatInput', 'input', autoGrow);
    bind('chatInput', 'keydown', (e) => {
      if (e.key === 'Escape') {
        const pop = $('chipLenPop');
        if (pop && pop.classList.contains('open')) { closeChipLen(); return; }
        if (ST.editFrom) setEditMode(null);
        return;
      }
      // 中文输入法组合中的 Enter 是「上屏」，不能当发送
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send('chat', undefined, ST.editFrom ? { editFrom: ST.editFrom } : undefined);
      }
    });
    bind('chatTemp', 'input', () => { if ($('lblTemp')) $('lblTemp').textContent = Number($('chatTemp').value).toFixed(2); });
    bind('chatTopP', 'input', () => { if ($('lblTopP')) $('lblTopP').textContent = Number($('chatTopP').value).toFixed(2); });
    bind('chatMaxTokens', 'input', () => {
      const e = $('chatMaxTokens');
      if (e) { e.dataset.touched = '1'; e.dataset.prev = e.value; }
      // 填 0 / 负数等价于「不限制」，直接把开关拨到 ON，避免两种写法互相打脸
      if (e && Number(e.value) <= 0 && $('chatUnlimited') && !$('chatUnlimited').checked) {
        $('chatUnlimited').checked = true;
        syncMaxTokens();
      }
    });
    bind('chatUnlimited', 'change', () => {
      const box = $('chatUnlimited');
      if (box) localStorage.setItem(LS_UNLIMITED, box.checked ? '1' : '0');
      syncMaxTokens();
    });
    bind('chatMemory', 'input', () => {
      const s = $('chatMemStatus');
      if (s && !s.textContent.includes('未保存')) s.textContent = '未保存';
    });
    const box = $('chatBox');
    if (box) box.addEventListener('click', (ev) => { onBoxAction(ev).catch((e) => toast(e.message, 'err')); });

    // 默认「不限制回答长度」（用户可关，选择记进 localStorage）
    const ub = $('chatUnlimited');
    if (ub) {
      const saved = localStorage.getItem(LS_UNLIMITED);
      ub.checked = saved === null ? true : saved === '1';
    }
    syncMaxTokens();
    autoGrow();

    loadSessions(0).catch((e) => toast(`会话加载失败：${e.message}`, 'err'));
  }

  function onPanel() {
    if (ST.cur) syncPanel();
  }

  function onConnectionsChanged() {
    if (ST.cur && $('chatConn') && ST.cur.conn_id) {
      $('chatConn').value = String(ST.cur.conn_id);
    }
    if ($('chatConn') && $('chatConn').value && typeof window.loadModelsInto === 'function') {
      const want = ST.cur ? ST.cur.model : '';
      window.loadModelsInto('chatModel', $('chatConn').value);
      if (want) {
        setTimeout(() => {
          if ($('chatModel') && Array.from($('chatModel').options).some((o) => o.value === want)) {
            $('chatModel').value = want;
          }
        }, 500);
      }
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  return {
    ST,
    init,
    onPanel,
    onConnectionsChanged,
    loadSessions,
    openSession,
    newSession,
    send,
    setMetrics,
    applyContext,
    renderMessages,
    mdRender,
    updateChipLen,
  };
})();

/* 顶层 const 只进全局词法作用域、不会挂到 window 上（踩过的坑），显式导出 */
window.CHAT = CHAT;
