/* =====================================================================
 * 富文本渲染器离线校验（不需要浏览器，也不需要跑服务）
 *
 * 为什么单独做一个：渲染器在 chat.js 的闭包里，页面上很难逐条断言。
 * 这里把源码切片抽出来在 Node 里直接跑，覆盖：
 *   标题 / 列表 / 任务 / 表格 / 代码块（闭合与流式未闭合）/ 行内标记 /
 *   引用 / 分隔线 / 链接白名单 / XSS 转义。
 *
 * 约定：stdout 只输出 JSON，进度与结论走 stderr。
 * ===================================================================== */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SRC = path.join(ROOT, 'frontend', 'chat.js');

const log = (...a) => process.stderr.write(a.join(' ') + '\n');
const results = [];
let failed = 0;

function check(name, cond, detail) {
  const ok = !!cond;
  if (!ok) failed += 1;
  results.push({ name, ok, detail: detail === undefined ? null : String(detail).slice(0, 400) });
  log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail !== undefined && !ok ? '  :: ' + String(detail).slice(0, 200) : ''}`);
}

/* ---------- 从 chat.js 抽出渲染器源码（两个稳定锚点之间） ---------- */
const src = fs.readFileSync(SRC, 'utf8');
const A = src.indexOf('/* ------------------------------ 富文本渲染');
const B = src.indexOf('/* 流式渲染节流');
if (A < 0 || B < 0 || B <= A) {
  console.error(JSON.stringify({ ok: false, error: 'ANCHOR_NOT_FOUND', A, B }));
  process.exit(2);
}
const chunk = src.slice(A, B);
const escSrc = "const esc = (s) => String(s == null ? '' : s).replace(/[&<>\"']/g, "
  + "(c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));";

let mdRender;
try {
  // eslint-disable-next-line no-new-func
  const factory = new Function(escSrc + '\n' + chunk + '\nreturn mdRender;');
  mdRender = factory();
} catch (e) {
  console.error(JSON.stringify({ ok: false, error: 'EVAL_FAILED', message: e.message }));
  process.exit(3);
}
log(`渲染器源码 ${chunk.length} 字符，加载成功`);

/* ---------- 1. 块级元素 ---------- */
check('标题 h1~h3', /<h1>一级<\/h1>[\s\S]*<h2>二级<\/h2>[\s\S]*<h3>三级<\/h3>/
  .test(mdRender('# 一级\n## 二级\n### 三级')));

const ul = mdRender('- 甲\n- 乙\n  - 乙一\n- 丙');
check('无序列表（含子层）',
  ul.includes('<ul>') && ul.includes('<li>甲</li>') && ul.includes('<li>乙一</li>'),
  ul);
check('无序列表子层嵌套在父 li 内', /<li>乙<ul><li>乙一<\/li><\/ul><\/li>/.test(ul), ul);

const ol = mdRender('1. 一\n2. 二\n3. 三');
check('有序列表', /<ol><li>一<\/li><li>二<\/li><li>三<\/li><\/ol>/.test(ol), ol);
check('有序列表 start 保留', mdRender('3. 三\n4. 四').includes('<ol start="3">'));

const task = mdRender('- [x] 已完成\n- [ ] 待办');
check('任务列表', task.includes('type="checkbox"') && task.includes('checked')
  && task.includes('待办'), task);

const tbl = mdRender('| 指标 | 值 |\n| --- | ---: |\n| TPS | 55.5 |\n| TTFT | 2.2 |');
check('表格解析', /<table><thead><tr><th>指标<\/th><th>值<\/th><\/tr><\/thead>/
  .test(tbl) && tbl.includes('<td>55.5</td>'), tbl);

check('引用', mdRender('> 第一行\n> 第二行').includes('<blockquote>'));
check('分隔线', mdRender('---').includes('<hr>'));

/* ---------- 2. 代码块 ---------- */
const cb = mdRender('说明：\n\n```python\nprint("hi")\n```');
check('代码块抽成独立外壳', cb.includes('class="cb"') && cb.includes('<code>')
  && cb.includes('data-lang="python"'), cb);
check('代码块带语言标签与复制按钮',
  cb.includes('<span class="cb-lang">python</span>') && cb.includes('data-act="copycode"'), cb);
check('代码块内 HTML 被转义', !cb.includes('<script>') && cb.includes('print(&quot;hi&quot;)'), cb);

const openFence = mdRender('看这段：\n\n```js\nconst a = 1;\nconst b = 2;');
check('流式未闭合围栏也渲成代码块（不闪断）',
  openFence.includes('class="cb"') && openFence.includes('const b = 2;'), openFence);
check('未闭合围栏不带残留的反引号', !openFence.includes('```'), openFence);

/* ---------- 3. 行内标记 ---------- */
const inl = mdRender('这是 **粗**、*斜*、~~删~~、==高亮==、`行内` 的混排。');
const inlOk = inl.includes('<b>粗</b>') && inl.includes('<i>斜</i>')
  && inl.includes('<del>删</del>') && inl.includes('<mark>高亮</mark>')
  && inl.includes('<code>行内</code>');
check('行内标记（粗/斜/删/高亮/代码）', inlOk, inl);
check('粗体会被解析为 b 而非字面星号', !inl.includes('**'), inl);

/* ---------- 4. 链接与安全 ---------- */
const link = mdRender('见 [文档](https://example.com/a) 与 https://example.com/b 。');
check('Markdown 链接', link.includes('<a href="https://example.com/a"'), link);
check('裸链接自动识别', link.includes('href="https://example.com/b"'), link);
check('链接带 noopener', link.includes('rel="noopener noreferrer"'), link);

const evil = mdRender('[点我](javascript:alert(1)) 和 <img src=x onerror=alert(2)>');
check('javascript: 伪协议被丢弃', !evil.includes('href="javascript'), evil);
check('裸 HTML 标签被转义（无注入）',
  !/<img/i.test(evil) && evil.includes('&lt;img'), evil);
check('script 标签被转义',
  mdRender('<script>alert(1)</script>').includes('&lt;script&gt;'));

/* ---------- 5. 段落与边界 ---------- */
const para = mdRender('第一行\n第二行\n\n新段落');
check('段内换行转 <br>，空行分段',
  /<p>第一行<br>第二行<\/p>[\s\S]*<p>新段落<\/p>/.test(para), para);
check('空输入返回空串', mdRender('') === '' && mdRender(null) === '');
check('纯文本不产生多余标签', mdRender('就是一句话').trim() === '<p>就是一句话</p>',
  mdRender('就是一句话'));

const big = Array.from({ length: 400 }, (_, i) => `第 ${i + 1} 行普通文本`).join('\n');
const t0 = Date.now();
const bigOut = mdRender(big);
const cost = Date.now() - t0;
check(`大文本渲染不退化（${bigOut.length} 字符，${cost}ms）`, bigOut.length > 4000 && cost < 1500,
  `${cost}ms`);

/* ---------- 输出 ---------- */
const out = {
  ok: failed === 0,
  total: results.length,
  passed: results.length - failed,
  failed,
  failures: results.filter((r) => !r.ok),
};
process.stdout.write(JSON.stringify(out, null, 2) + '\n');
log(`\n${out.passed}/${out.total} 通过，${failed} 失败`);
process.exit(failed === 0 ? 0 : 1);
