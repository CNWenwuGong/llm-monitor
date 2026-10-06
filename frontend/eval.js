/* =====================================================================
 * 评测中心 · 前端逻辑
 * 依赖 app.js 顶层的 S / $ / api / fmt / esc / toast / logLine（经典脚本共享全局词法作用域）
 * ===================================================================== */
'use strict';

const EVAL = (() => {
  const ST = {
    groups: [],           // 套件分组
    modes: {},
    mode: 'quick',
    suiteId: '',
    runId: null,
    running: false,
    catalog: null,
    eff: null,
    loaded: { suites: false, catalog: false, eff: false },
  };

  const $id = (x) => document.getElementById(x);

  /* ---------------- 工具 ---------------- */
  const statusMeta = {
    builtin: { cls: 'green', text: '已内置' },
    partial: { cls: 'yellow', text: '部分支持' },
    planned: { cls: 'red', text: '未内置' },
  };

  function suiteById(id) {
    for (const g of ST.groups) {
      const s = g.suites.find((x) => x.id === id);
      if (s) return { ...s, category: g.category, categoryLabel: g.label };
    }
    return null;
  }

  function metricCard(label, value, unit, sub, kind) {
    return `<div class="kpi ${kind || ''}">
      <div class="label">${esc(label)}</div>
      <div class="value">${value}${unit ? `<span class="unit">${esc(unit)}</span>` : ''}</div>
      ${sub ? `<div class="sub">${sub}</div>` : ''}
    </div>`;
  }

  function pct(v) { return (v === null || v === undefined) ? '—' : `${(v * 100).toFixed(1)}%`; }

  /* ---------------- 套件清单 ---------------- */
  async function loadSuites(force) {
    if (ST.loaded.suites && !force) return;
    try {
      const d = await api('GET', '/api/eval/suites');
      ST.groups = d.groups || [];
      ST.modes = d.modes || {};
      ST.loaded.suites = true;
      renderModes();
      renderSuiteList();
      const first = ST.suiteId || (ST.groups[0] && ST.groups[0].suites[0] && ST.groups[0].suites[0].id);
      if (first) selectSuite(first);
    } catch (e) {
      $id('evSuiteList').innerHTML = `<div class="empty">套件加载失败：${esc(e.message)}</div>`;
    }
  }

  function renderModes() {
    const sel = $id('evMode');
    const cur = ST.mode;
    sel.innerHTML = Object.entries(ST.modes)
      .map(([k, v]) => `<option value="${k}" ${k === cur ? 'selected' : ''}>${esc(v.label || k)}</option>`)
      .join('');
  }

  function renderSuiteList() {
    const host = $id('evSuiteList');
    if (!ST.groups.length) { host.innerHTML = '<div class="empty">没有可用套件</div>'; return; }
    host.innerHTML = ST.groups.map((g) => `
      <div class="ev-group">
        <div class="ev-group-head">
          <b>${esc(g.label)}</b>
          <span class="muted small">${esc(g.desc || '')}</span>
        </div>
        <div class="ev-suites">
          ${g.suites.map((s) => `
            <button class="ev-suite ${s.id === ST.suiteId ? 'sel' : ''}" data-suite="${esc(s.id)}">
              <span class="ev-suite-name">${esc(s.name)}</span>
              <span class="ev-suite-meta">
                <code>${esc(s.id)}</code>
                <span class="muted">${esc(s.metric)}</span>
              </span>
              <span class="ev-suite-foot">
                <span class="muted small">题量 ${s.total_items}</span>
                ${s.needs_code_exec ? '<span class="badge yellow">需执行代码</span>' : ''}
              </span>
            </button>`).join('')}
        </div>
      </div>`).join('');
    host.querySelectorAll('.ev-suite').forEach((b) => {
      b.onclick = () => selectSuite(b.dataset.suite);
    });
  }

  function selectSuite(id) {
    ST.suiteId = id;
    const s = suiteById(id);
    document.querySelectorAll('.ev-suite').forEach((b) => b.classList.toggle('sel', b.dataset.suite === id));
    if (!s) return;
    const modeCount = (s.modes && s.modes[ST.mode]) || s.total_items;
    $id('evSuiteDesc').innerHTML =
      `<b>${esc(s.name)}</b>（${esc(s.categoryLabel)}）<br>判分口径：<code>${esc(s.metric)}</code> · ` +
      `本次约 <b>${modeCount}</b> 题${s.needs_code_exec ? ' · <span style="color:var(--yellow)">需勾选「允许执行代码」</span>' : ''}` +
      (s.desc ? `<br>${esc(s.desc)}` : '');
    if (s.needs_code_exec) $id('evCodeExec').checked = $id('evCodeExec').checked || false;
    updateRunButton();
    estimate();
  }

  async function estimate() {
    const s = suiteById(ST.suiteId);
    if (!s) { $id('evEstimate').textContent = ''; return; }
    try {
      const q = `suite_id=${encodeURIComponent(ST.suiteId)}&mode=${encodeURIComponent(ST.mode)}`;
      const d = await api('GET', `/api/eval/estimate?${q}`);
      if (!d.ok) { $id('evEstimate').textContent = ''; return; }
      $id('evEstimate').textContent =
        `预估：${d.items} 题 · Prompt 约 ${fmtInt(d.prompt_tokens)} tok · 生成约 ${fmtInt(d.completion_tokens_est)} tok` +
        ` · 按本机速率约 ${d.seconds_est} s（${d.assumes}）`;
    } catch (e) { $id('evEstimate').textContent = ''; }
  }

  function updateRunButton() {
    const s = suiteById(ST.suiteId);
    const needCode = !!(s && s.needs_code_exec);
    const ok = !!s && (!needCode || $id('evCodeExec').checked);
    $id('btnRunEval').disabled = ST.running || !ok;
    $id('btnRunEval').textContent = needCode && !$id('evCodeExec').checked
      ? '需先勾选「允许执行代码」' : '开始评测';
  }

  /* ---------------- 启动 / 停止 ---------------- */
  async function start() {
    const connId = $id('evConn').value;
    if (!connId) { toast('请先选择连接档案', 'warn'); return; }
    if (!ST.suiteId) { toast('请先选择评测套件', 'warn'); return; }
    const body = {
      conn_id: Number(connId),
      model: $id('evModel').value || '',
      suite_id: ST.suiteId,
      mode: ST.mode,
      allow_code_exec: !!$id('evCodeExec').checked,
      judge_conn_id: $id('evJudge').value ? Number($id('evJudge').value) : null,
      judge_model: $id('evJudge').value ? ($id('evModel').value || '') : '',
    };
    try {
      const d = await api('POST', '/api/eval/start', body);
      ST.runId = d.run_id;
      ST.running = true;
      setRunning(true);
      renderProgress({ suite: ST.suiteId, mode: ST.mode, index: 0, total: 0, current: '排队中…', graded: 0, correct: 0 });
      logLine('EVAL', `评测启动 Run #${d.run_id} · 套件 ${ST.suiteId} · 模式 ${ST.mode}`, 'info');
      toast(`评测已启动（Run #${d.run_id}）`, 'ok');
    } catch (e) { toast(e.message, 'err'); }
  }

  async function stop() {
    if (!ST.runId) return;
    try { await api('POST', '/api/test/stop', { run_id: ST.runId }); toast('已请求停止', 'warn'); }
    catch (e) { toast(e.message, 'err'); }
  }

  function setRunning(on) {
    ST.running = on;
    $id('btnStopEval').disabled = !on;
    updateRunButton();
  }

  /* ---------------- 进度 ---------------- */
  function renderProgress(p) {
    const total = p.total || 0;
    const done = p.index || 0;
    const ratio = total ? Math.min(1, done / total) : 0;
    const cls = p.graded ? 'green' : '';
    $id('evProgress').innerHTML = `
      <div class="small muted">${esc(p.current || '')}</div>
      <div class="bar-outer"><div class="bar-inner ${cls}" style="width:${(ratio * 100).toFixed(1)}%"></div></div>
      <div class="small muted mt8">已跑 ${done} / ${total || '?'} 题 · 已判分 ${p.graded || 0} · 判定正确 ${p.correct || 0}</div>`;
  }

  /* ---------------- 结果渲染 ---------------- */
  function renderSummary(ev, run) {
    if (!ev) { $id('evSummary').innerHTML = '<div class="empty">尚未运行评测</div>'; return; }
    $id('evResultHint').textContent = `${ev.suite_name || ev.suite_id || ''} · ${ST.modes[ev.mode] ? ST.modes[ev.mode].label : (ev.mode || '')}`;
    const cards = [];
    const P = (v) => (v === null || v === undefined) ? '—' : (v * 100).toFixed(1) + '%';

    switch (ev.metric) {
      case 'accuracy_mc':
      case 'accuracy_numeric':
        cards.push(metricCard('准确率', P(ev.accuracy), '', `${ev.correct || 0} / ${ev.graded || 0} 题正确`, 'green'));
        if (ev.hallucination_rate !== undefined && ev.hallucination_rate !== null) {
          cards.push(metricCard('幻觉率', P(ev.hallucination_rate), '', 'TruthfulQA 口径：1 − 准确率', 'red'));
        }
        break;
      case 'niah':
        cards.push(metricCard('检索命中率', P(ev.accuracy), '', `${ev.correct || 0} / ${ev.graded || 0}`, 'cyan'));
        break;
      case 'pass_at_k':
        cards.push(metricCard('pass@1', P(ev.pass_at_1), '', `${ev.solved || 0} 题通过单测`, 'green'));
        cards.push(metricCard(ev.pass_at_k_label || 'pass@k', P(ev.pass_at_k), '', `每题采样 ${ev.k_samples || 1} 次（无偏估计）`, 'purple'));
        if (ev.blocked) cards.push(metricCard('沙箱拦截', fmtInt(ev.blocked), '次', '命中危险性代码黑名单', 'red'));
        break;
      case 'constraint_rate':
        cards.push(metricCard('指令遵循率', P(ev.instruction_rate), '', '整题全部约束通过的比例', 'green'));
        cards.push(metricCard('单条约束通过率', P(ev.check_pass_rate), '', '逐条约束粒度', 'cyan'));
        break;
      case 'rouge':
        cards.push(metricCard('ROUGE-1', fmt(ev.rouge1, 3), '', '', 'green'));
        cards.push(metricCard('ROUGE-2', fmt(ev.rouge2, 3), '', '', 'green'));
        cards.push(metricCard('ROUGE-L', fmt(ev.rougeL, 3), '', '', 'cyan'));
        cards.push(metricCard('BLEU', fmt(ev.bleu, 3), '', 'method2 平滑', 'purple'));
        cards.push(metricCard('CHRF++', fmt(ev.chrf, 3), '', '字符+词 n-gram，β=2', 'purple'));
        break;
      case 'f1_extract':
        cards.push(metricCard('F1', fmt(ev.f1, 3), '', '', 'green'));
        cards.push(metricCard('精确率', fmt(ev.precision, 3), '', '', 'cyan'));
        cards.push(metricCard('召回率', fmt(ev.recall, 3), '', '', 'cyan'));
        break;
      case 'refusal_rate':
        cards.push(metricCard('有害请求拒绝率', P(ev.refusal_rate), '', '越高越好', 'green'));
        cards.push(metricCard('危险内容泄露率', P(ev.harmful_leak_rate), '', '越低越好', 'red'));
        break;
      case 'jailbreak_rate':
        cards.push(metricCard('越狱成功率', P(ev.jailbreak_rate), '', '越低越好', 'red'));
        cards.push(metricCard('护栏守住率', P(ev.guard_rate), '', '越高越好', 'green'));
        break;
      case 'judge_score':
        cards.push(metricCard('综合得分', fmt(ev.score, 2), '', esc(ev.score_source || ''), 'purple'));
        if (ev.rubric_coverage !== null && ev.rubric_coverage !== undefined) {
          cards.push(metricCard('评分要点覆盖', P(ev.rubric_coverage), '', '关键词命中比例', 'cyan'));
        }
        break;
      default:
        break;
    }
    // 效率侧（每次评测都产出）
    cards.push(metricCard('平均 TTFT', fmt(ev.avg_ttft_ms, 0), 'ms', '首 Token 延迟', 'blue'));
    cards.push(metricCard('P95 端到端', fmt(ev.p95_e2e_ms, 0), 'ms', `平均 ${fmt(ev.avg_e2e_ms, 0)} ms`, 'blue'));
    cards.push(metricCard('Prefill 吞吐', fmt(ev.avg_prefill_tps, 1), 'tok/s', '算力密集段', 'yellow'));
    cards.push(metricCard('Decode 吞吐', fmt(ev.avg_decode_tps, 1), 'tok/s', '访存瓶颈段', 'yellow'));

    const extra = [];
    if (ev.constraint_failures && Object.keys(ev.constraint_failures).length) {
      extra.push(`<div class="metric-item"><span class="k">未通过的约束类型</span><span class="v">${
        Object.entries(ev.constraint_failures).map(([k, v]) => `${esc(k)}×${v}`).join('、')}</span></div>`);
    }
    if (ev.by_length && Object.keys(ev.by_length).length) {
      extra.push(`<div class="metric-item"><span class="k">按上下文长度命中率</span><span class="v">${
        Object.entries(ev.by_length).map(([k, v]) => `${esc(k)} tok → ${P(v)}`).join(' · ')}</span></div>`);
    }
    if (ev.note) extra.push(`<div class="small muted mt8">${esc(ev.note)}</div>`);
    if (run && run.status === 'stopped') {
      extra.push(`<div class="small mt8" style="color:var(--yellow)">本次评测被提前停止，指标仅基于已完成的题。</div>`);
    }

    $id('evSummary').innerHTML =
      `<div class="row r4" style="margin-bottom:12px">${cards.join('')}</div>` +
      `<div class="row r4" style="margin-bottom:0">
         <div class="metric-item"><span class="k">题量</span><span class="v">${ev.total || 0}</span></div>
         <div class="metric-item"><span class="k">判分</span><span class="v">${ev.graded || 0}</span></div>
         <div class="metric-item"><span class="k">请求失败</span><span class="v">${ev.errors || 0}</span></div>
         <div class="metric-item"><span class="k">耗时</span><span class="v">${fmt((ev.duration_ms || 0) / 1000, 1)} s</span></div>
       </div>` + extra.join('');
  }

  function renderItems(items) {
    const tb = $id('evItemTable');
    if (!items || !items.length) { tb.innerHTML = '<tr><td colspan="8" class="empty">尚无题目明细</td></tr>'; return; }
    tb.innerHTML = items.map((it) => {
      const ok = it.ok === 1 || it.ok === true;
      const ans = String(it.answer || '').replace(/\n/g, ' ');
      return `<tr>
        <td class="nowrap"><code>${esc(it.item_id)}</code></td>
        <td class="nowrap">${esc(it.case_label || '')}</td>
        <td class="nowrap"><span class="badge ${ok ? 'green' : 'red'}">${ok ? '通过' : '未通过'}</span></td>
        <td>${esc(it.detail || '')}${it.error ? `<br><span style="color:var(--red)">${esc(it.error)}</span>` : ''}</td>
        <td class="num">${fmt(it.ttft_ms, 0)}</td>
        <td class="num">${fmt(it.e2e_ms, 0)}</td>
        <td class="num">${fmt(it.decode_tps, 1)}</td>
        <td class="ev-ans" title="${esc(ans)}">${esc(ans.slice(0, 90))}${ans.length > 90 ? '…' : ''}</td>
      </tr>`;
    }).join('');
  }

  /* ---------------- 结果加载 ---------------- */
  async function loadResult(runId) {
    try {
      const d = await api('GET', `/api/eval/${runId}`);
      ST.runId = runId;
      renderSummary(d.summary, d.run);
      renderItems(d.items);
      ['btnEvCsv', 'btnEvJson', 'btnEvReport'].forEach((b) => { $id(b).disabled = false; });
      return d;
    } catch (e) { toast(e.message, 'err'); return null; }
  }

  /* ---------------- 指标覆盖清单 ---------------- */
  async function loadCatalog(force) {
    if (ST.loaded.catalog && !force) return;
    try {
      const d = await api('GET', '/api/metrics/catalog');
      ST.catalog = d;
      ST.loaded.catalog = true;
      renderCatalog(d);
    } catch (e) {
      $id('evCatalog').innerHTML = `<div class="empty">指标清单加载失败：${esc(e.message)}</div>`;
    }
  }

  function renderCatalog(d) {
    const s = d.summary || {};
    $id('evCoverHint').innerHTML =
      `共登记 <b>${s.total}</b> 项，已覆盖 <b style="color:var(--green)">${s.covered}</b> 项` +
      `（已内置 ${s.builtin} · 部分 ${s.partial}）· 未内置 ${s.planned} · 覆盖率 ${pct(s.coverage)}`;
    $id('evCatalog').innerHTML = d.groups.map((g) => `
      <div class="ev-cat">
        <div class="ev-cat-head">
          <b>${esc(g.label)}</b>
          <span class="badge green">已内置 ${g.builtin}</span>
          ${g.partial ? `<span class="badge yellow">部分 ${g.partial}</span>` : ''}
          ${g.planned ? `<span class="badge red">未内置 ${g.planned}</span>` : ''}
          <span class="muted small">${esc(g.desc || '')}</span>
        </div>
        <div class="ev-metrics">
          ${g.metrics.map((m) => {
            const sm = statusMeta[m.status] || statusMeta.planned;
            return `<div class="ev-metric" title="${esc(m.how)}">
              <div class="ev-metric-top">
                <span class="ev-metric-name">${esc(m.name)}</span>
                <span class="badge ${sm.cls}">${sm.text}</span>
              </div>
              <div class="ev-metric-en muted small">${esc(m.en)}</div>
              <div class="ev-metric-how small">${esc(m.how)}</div>
              <div class="ev-metric-src small muted">${esc(m.source)}${m.suite ? ` → <code>${esc(m.suite)}</code>` : ''}</div>
            </div>`;
          }).join('')}
        </div>
      </div>`).join('');
  }

  /* ---------------- 效率与资源指标 ---------------- */
  async function loadEfficiency(force) {
    const connId = $id('evConn').value;
    if (!connId) { $id('evEff').innerHTML = '<div class="empty">请选择连接档案后刷新</div>'; return; }
    $id('evEff').innerHTML = '<div class="empty">采集中…</div>';
    try {
      const q = `conn_id=${connId}&model=${encodeURIComponent($id('evModel').value || '')}`;
      const d = await api('GET', `/api/metrics/efficiency?${q}`);
      ST.eff = d;
      ST.loaded.eff = true;
      renderEfficiency(d);
    } catch (e) {
      $id('evEff').innerHTML = `<div class="empty">采集失败：${esc(e.message)}</div>`;
    }
  }

  function num(v, d, unit) { return v === null || v === undefined ? '—' : `${fmt(v, d)}${unit || ''}`; }
  function mib(v) { return v === null || v === undefined ? '—' : `${fmt(v, 1)} MiB`; }

  function renderEfficiency(d) {
    const m = d.model || {};
    const kv = d.kv_cache || {};
    const fl = d.flops || {};
    const rows = [];

    const add = (k, v, note) => rows.push(
      `<div class="metric-item"><span class="k">${esc(k)}${note ? `<br><span class="small muted">${esc(note)}</span>` : ''}</span><span class="v">${v}</span></div>`);

    // 模型名 + 架构名。拆成两句写，避免把三元嵌进模板串里——
    // 之前这里就是「内层三元缺 else」和「外层三元缺 else」挤在一行，肉眼看不出来。
    const archTag = m.architecture ? ` <span class="small muted">${esc(m.architecture)}</span>` : '';
    const modelLabel = m.name ? `<code>${esc(m.name)}</code>${archTag}` : '—';
    add('模型', modelLabel, d.kind ? `后端 ${esc(d.kind)}` : '');

    add('PPL 支持', d.ppl_supported
      ? '<span class="badge green">支持</span>'
      : '<span class="badge red">不支持</span>', d.ppl_supported ? '' : '该后端未暴露对数概率接口，不做臆测填充');

    add('上下文窗口', num(m.context_length, 0, ' tokens'), m.context_source || '');
    add('参数量', m.params_b ? `${fmt(m.params_b, 2)} B` : '—', m.parameter_count_source || '');
    add('权重显存', mib(m.weights_mib), m.quantization ? `量化 ${esc(m.quantization)}` : '');
    add('层数 / KV 头 / 头维', `${num(m.n_layer, 0)} / ${num(m.n_head_kv, 0)} / ${num(m.head_dim, 0)}`,
      'KV Cache 折算的三个关键参数');
    if (m.hidden_size || m.vocab_size) {
      add('隐藏维度 / 词表', `${num(m.hidden_size, 0)} / ${num(m.vocab_size, 0)}`, '与参数量、FLOPs 交叉核对用');
    }
    add('KV Cache（单槽）', mib(kv.per_slot_mib),
      kv.kv_type
        ? `${esc(kv.kv_type)} · ${num(kv.n_ctx, 0)} ctx × ${num(kv.n_layer, 0)} 层 × ${num(m.n_head_kv, 0)} KV头 × ${num(m.head_dim, 0)} 维`
        : '');
    add('KV Cache（合计）', mib(kv.total_mib), kv.slots ? `${kv.slots} 个槽位（--parallel）` : '');
    if (m.total_vram_mib !== undefined && m.total_vram_mib !== null) {
      add('合计显存估算', mib(m.total_vram_mib), '权重 + KV Cache');
    }
    if (fl.flops_per_token) add('每 Token FLOPs', `${fmt(fl.flops_per_token / 1e9, 2)} GFLOPs`, fl.formula || '');
    if (fl.theoretical_tflops_1s) add('理论算力口径', `${fmt(fl.theoretical_tflops_1s, 3)} TFLOPs`, '2·N，可与实测对照算 MFU');

    let html = `<div class="metric-list">${rows.join('')}</div>`;

    if ((kv.table || []).length) {
      html += `<h3 class="ev-sub">KV Cache 随上下文增长（容量规划参考）</h3>
        <div class="table-wrap"><table>
          <thead><tr><th class="num">上下文长度</th><th class="num">单槽 KV</th><th class="num">${kv.slots || 1} 槽合计</th></tr></thead>
          <tbody>${kv.table.map((r) => `<tr>
            <td class="num">${fmtInt(r.ctx)}</td>
            <td class="num">${fmt(r.per_slot_gib, 2)} GiB</td>
            <td class="num">${fmt(r.slots_total_gib, 2)} GiB</td>
          </tr>`).join('')}</tbody>
        </table></div>`;
    }

    html += `<h3 class="ev-sub">原生指标</h3>`;
    html += `<pre class="ev-json">${esc(JSON.stringify(d.native || {}, null, 2))}</pre>`;
    if (d.simulated) {
      html = `<div class="badge yellow" style="margin-bottom:10px">模拟数据 · 公式真实、输入为演示档位</div>` + html;
    }
    if ((d.notes || []).length) {
      html += `<div class="small mt12" style="color:var(--yellow)">${d.notes.map(esc).join('<br>')}</div>`;
    }
    $id('evEff').innerHTML = html;
  }

  async function coldStart() {
    const connId = $id('evConn').value;
    if (!connId) { toast('请先选择连接档案', 'warn'); return; }
    const btn = $id('btnColdStart');
    btn.disabled = true; btn.textContent = '探测中…';
    try {
      const d = await api('POST', '/api/metrics/cold-start',
        { conn_id: Number(connId), model: $id('evModel').value || '' });
      if (!d.ok) {
        $id('evCold').innerHTML = `<div class="empty">无法测量：${esc(d.reason || '未知原因')}</div>`;
      } else {
        const items = [
          ['后端 / 模型', `${esc(d.backend)} / <code>${esc(d.model || '—')}</code>`],
          ['加载耗时', d.load_ms !== null && d.load_ms !== undefined ? `${fmt(d.load_ms, 0)} ms` : '—（无法测量）'],
          ['测量方式', esc(d.measured_by || '')],
        ];
        if (d.total_ms !== undefined) items.push(['总耗时', `${fmt(d.total_ms, 0)} ms`]);
        if (d.first_request_ttft_ms !== undefined) items.push(['首请求 TTFT', `${fmt(d.first_request_ttft_ms, 0)} ms`]);
        $id('evCold').innerHTML =
          `<div class="metric-list">${items.map(([k, v]) =>
            `<div class="metric-item"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('')}</div>` +
          (d.simulated ? '<div class="small mt12" style="color:var(--yellow)">内置演示后端的合成值，不代表真实模型。</div>' : '') +
          (d.reason ? `<div class="small mt12 muted">${esc(d.reason)}</div>` : '');
      }
    } catch (e) { toast(e.message, 'err'); }
    btn.disabled = false; btn.textContent = '开始探测';
  }

  /* ---------------- WS 钩子 ---------------- */
  function onProgress(msg) {
    setRunning(true);
    $id('evProgress').innerHTML = '';
    if (msg.eval) renderProgress(msg.eval);
    if (msg.last) {
      logLine('EVAL', `${msg.last.label || msg.last.id} → ${msg.last.ok ? '通过' : '未通过'}${msg.last.detail ? '：' + msg.last.detail : ''}`,
        msg.last.ok ? 'ok' : 'warn');
    }
  }

  async function onRunDone(msg) {
    setRunning(false);
    if (msg.status === 'error') {
      $id('evProgress').innerHTML = `<span class="badge red">评测失败</span>`;
      if (msg.error) toast(`评测失败：${esc(msg.error)}`, 'err', 7000);
      return;
    }
    $id('evProgress').innerHTML =
      `<span class="badge ${msg.status === 'done' ? 'green' : 'yellow'}">评测${msg.status === 'done' ? '完成' : '已停止'}</span>`;
    await loadResult(msg.run_id);
    logLine('EVAL', `评测 ${msg.status === 'done' ? '完成' : '停止'} · Run #${msg.run_id}`, msg.status === 'done' ? 'ok' : 'warn');
    toast(`评测${msg.status === 'done' ? '完成' : '已停止'}，结果已落库 Run #${msg.run_id}`, msg.status === 'done' ? 'ok' : 'warn');
  }

  function onHello(r) {
    setRunning(true);
    ST.runId = r.run_id;
    logLine('EVAL', `检测到进行中的评测 Run #${r.run_id}`, 'info');
  }

  /* ---------------- 面板激活 ---------------- */
  async function onPanel() {
    if (!ST.modes || !Object.keys(ST.modes).length) await loadSuites();
    loadModelsInto('evModel', $id('evConn').value);
    loadCatalog();
    loadEfficiency();
    if (ST.runId) loadResult(ST.runId);
  }

  /* ---------------- 导出 ---------------- */
  function download(path, filename) {
    const a = document.createElement('a');
    a.href = path;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  /* ---------------- 事件绑定 ---------------- */
  function init() {
    $id('evMode').onchange = () => { ST.mode = $id('evMode').value; selectSuite(ST.suiteId); };
    $id('evConn').onchange = () => { loadModelsInto('evModel', $id('evConn').value); loadEfficiency(true); };
    $id('evCodeExec').onchange = updateRunButton;
    $id('btnRunEval').onclick = start;
    $id('btnStopEval').onclick = stop;
    $id('btnEffReload').onclick = () => loadEfficiency(true);
    $id('btnColdStart').onclick = coldStart;
    $id('btnEvCsv').onclick = () => download(`/api/eval/${ST.runId}/export?fmt=csv`, `llm-monitor-eval-${ST.runId}.csv`);
    $id('btnEvJson').onclick = () => download(`/api/eval/${ST.runId}/export?fmt=json`, `llm-monitor-eval-${ST.runId}.json`);
    $id('btnEvReport').onclick = async () => {
      if (!ST.runId) return;
      try {
        const d = await api('GET', `/api/runs/${ST.runId}/report`);
        openModal(`评测报告 · Run #${ST.runId}`, `<pre>${esc(d.markdown)}</pre>`, d.markdown);
      } catch (e) { toast(e.message, 'err'); }
    };
  }

  document.addEventListener('DOMContentLoaded', init);
  if (document.readyState !== 'loading') init();

  return { onPanel, onProgress, onRunDone, onHello, ST, loadSuites };
})();

// app.js 用 window.EVAL 做存在性判断，而顶层 const 只进全局词法作用域、
// 不会变成 window 的属性，所以必须显式挂一次，否则所有钩子都会被静默跳过。
window.EVAL = EVAL;
