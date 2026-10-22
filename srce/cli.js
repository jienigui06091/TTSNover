'use strict';
/**
 * 书源规则引擎 CLI：stdin 读 JSON 任务 → stdout 输出 JSON
 * {"cmd":"search","key":"剑来","sources":[{...}],"page":1}
 * {"cmd":"info","source":{...},"bookUrl":"..."}
 * {"cmd":"toc","source":{...},"bookUrl":"...","tocUrl":"..."}
 * {"cmd":"content","source":{...},"chapterUrl":"...","bookUrl":"..."}
 * {"cmd":"test","source":{...}}
 */
const E = require('./engine');
const { execFile } = require('child_process');

function load(body, contentType, url) {
  const txt = String(body || '');
  const isJson = /^\s*[\[{]/.test(txt) || /json/i.test(contentType || '');
  if (isJson) {
    try { return { doc: null, result: JSON.parse(txt), baseUrl: url }; } catch { /* fallthrough */ }
  }
  return { doc: E.cheerio.load(txt), result: txt, baseUrl: url };
}

function ctxOf(src, page, loaded, extra) {
  return Object.assign({
    doc: loaded.doc, result: loaded.result, baseUrl: loaded.baseUrl,
    scope: null, source: src, page: page || 1, vars: {},
  }, extra || {});
}

const pick = (rule, ctx, fallbackDoc) => {
  if (!rule || !String(rule).trim()) return '';
  const c = Object.assign({}, ctx, { doc: ctx.doc || fallbackDoc || null });
  try { return E.evalRule(rule, c.result, c); } catch (e) { return ''; }
};

async function fetchPage(url, src, ctx) {
  E.ensureSource(src);
  const c = ctx || { result: null, baseUrl: src.bookSourceUrl, vars: {}, source: src };
  const res = await E.fetchRule(url, c, src);
  if (res.error) return { error: res.error, code: 0 };
  return { ...load(res.body, res.contentType, res.url || url), code: res.code, finalUrl: res.url || url };
}

/* ---------------- 搜索 ---------------- */
async function cmdSearch(job) {
  const key = job.key || '';
  const page = job.page || 1;
  const srcs = job.sources || [];

  // 单源：直接跑（子进程模式，父进程会硬超时杀掉）
  if (srcs.length <= 1) {
    const src = srcs[0];
    if (!src) return { results: [] };
    try {
      const r = await E.doSearch(src, key, page);
      if (r.error) return { results: [{ source: src.bookSourceName, error: r.error, url: r.url }] };
      return { results: [{ source: src.bookSourceName, books: r.books, url: r.url }] };
    } catch (e) {
      return { results: [{ source: src.bookSourceName, error: String((e && e.message) || e) }] };
    }
  }

  // 多源：每个源一个子进程，卡死/超时都不影响别的源
  const one = (src) => new Promise((resolve) => {
    const child = execFile('node', [__filename], { timeout: 9000, maxBuffer: 32 * 1024 * 1024 }, (err, stdout) => {
      if (!stdout) {
        return resolve({ source: src.bookSourceName, error: err && err.killed ? '超时 9s' : '进程失败' });
      }
      try {
        const j = JSON.parse(stdout);
        resolve((j.results && j.results[0]) || { source: src.bookSourceName, error: '空结果' });
      } catch {
        resolve({ source: src.bookSourceName, error: '输出解析失败' });
      }
    });
    child.stdin.end(JSON.stringify({ cmd: 'search', key, page, sources: [src] }));
  });

  // 并发上限：每个源一个 node 子进程（约 70MB），全开会把小内存云服务器打死。
  // SEARCH_CONC 默认 10（约 700MB 峰值），本机可调到 24。
  const CONC = Math.max(1, parseInt(process.env.SEARCH_CONC || '10', 10));
  const results = new Array(srcs.length);
  let cursor = 0;
  const worker = async () => {
    while (cursor < srcs.length) {
      const idx = cursor++;
      results[idx] = await one(srcs[idx]);
    }
  };
  await Promise.all(Array.from({ length: Math.min(CONC, srcs.length) }, worker));
  return { results };
}

/* ---------------- 详情 ---------------- */
async function cmdInfo(job) {
  const src = job.source;
  const bookUrl = job.bookUrl;
  const rs = src.ruleBookInfo || {};
  const pg = await fetchPage(bookUrl, src);
  if (pg.error) return { error: pg.error };
  if (pg.code >= 400) return { error: `HTTP ${pg.code}` };
  let ctx = ctxOf(src, 1, pg);
  // init：把范围缩到某个节点/对象
  const initRule = (rs.init || '').trim();
  let scope = null;
  if (initRule) {
    const v = pick(initRule, ctx);
    if (typeof v === 'string') {
      // 返回 HTML 片段
      const $ = E.cheerio.load(v);
      ctx = { ...ctx, doc: $, result: v };
      scope = [$('body').children().first()[0]].filter(Boolean);
    } else if (Array.isArray(v)) {
      const first = v[0];
      if (first && first.tagName) scope = [first];
      else ctx = { ...ctx, result: first, doc: null };
    } else if (v && typeof v === 'object') {
      if (v.tagName) scope = [v];
      else ctx = { ...ctx, result: v, doc: null };
    }
  }
  ctx = { ...ctx, scope };
  const get = (k) => E.str(pick(rs[k], ctx));
  const info = {
    name: get('name'),
    author: get('author'),
    kind: get('kind'),
    intro: get('intro'),
    coverUrl: E.absUrl(get('coverUrl'), ctx.baseUrl),
    lastChapter: get('lastChapter'),
    wordCount: get('wordCount'),
    tocUrl: E.absUrl(E.str(pick(rs.tocUrl, ctx)) || bookUrl, ctx.baseUrl),
  };
  return { info, bookUrl, finalUrl: ctx.baseUrl };
}

/* ---------------- 目录 ---------------- */
async function cmdToc(job) {
  const src = job.source;
  const rt = src.ruleToc || {};
  const tocUrl = job.tocUrl || job.bookUrl;
  const pg = await fetchPage(tocUrl, src);
  if (pg.error) return { error: pg.error };
  if (pg.code >= 400) return { error: `HTTP ${pg.code}` };
  let ctx = ctxOf(src, 1, pg);
  // init / preUpdateJs 忽略
  const listRule = (rt.chapterList || '').trim();
  let items = [];
  if (listRule) {
    const raw = pick(listRule, ctx);
    if (typeof raw === 'string') {
      if (pg.doc) {
        try { items = pg.doc(raw).toArray(); } catch { items = []; }
      }
    } else {
      items = E.asList(raw);
    }
  }
  const chapters = [];
  for (const item of items) {
    if (item === null || item === undefined) continue;
    const isEl = item && typeof item === 'object' && item.tagName;
    const ictx = { ...ctx, scope: isEl ? [item] : null, result: isEl ? item.toString() : item };
    const name = E.str(pick(rt.chapterName, ictx)) || E.str(isEl ? item.text : '');
    let url = E.str(pick(rt.chapterUrl, ictx));
    if (!name) continue;
    url = E.absUrl(url, pg.baseUrl);
    chapters.push({ name, url, isVip: E.str(pick(rt.isVip, ictx)) });
  }
  // 去重（保留最后一次出现，丢掉顶部「最新章节」块），再判断是否倒序
  const seen = new Set();
  const uniq = [];
  for (let i = chapters.length - 1; i >= 0; i--) {
    const key = chapters[i].url || chapters[i].name;
    if (seen.has(key)) continue;
    seen.add(key);
    uniq.push(chapters[i]);
  }
  uniq.reverse();
  chapters.length = 0;
  chapters.push(...uniq);

  const numOf = (s) => { const m = String(s || '').match(/([0-9]{1,5})/); return m ? +m[1] : null; };
  const nums = chapters.slice(0, 30).map((c) => numOf(c.name)).filter((x) => x !== null);
  if (nums.length >= 3) {
    let desc = 0;
    for (let i = 1; i < nums.length; i++) if (nums[i] < nums[i - 1]) desc++;
    if (desc >= (nums.length - 1) * 0.85) chapters.reverse();
  }
  return { chapters, tocUrl: pg.baseUrl, total: chapters.length };
}

/* ---------------- 正文 ---------------- */
function cleanText(html) {
  if (!html) return '';
  let s = String(html);
  s = s.replace(/<br\s*\/?>/gi, '\n').replace(/<\/p>/gi, '\n\n');
  s = s.replace(/<script[\s\S]*?<\/script>/gi, '').replace(/<style[\s\S]*?<\/style>/gi, '');
  s = s.replace(/<[^>]+>/g, '');
  s = s.replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/&ldquo;/g, '“').replace(/&rdquo;/g, '”');
  s = s.replace(/[ \t\u00a0]+/g, ' ').replace(/\s*\n\s*/g, '\n');
  return s.trim();
}

async function cmdContent(job) {
  const src = job.source;
  const rc = src.ruleContent || {};
  let url = job.chapterUrl;
  const parts = [];
  let title = '';
  for (let page = 0; page < 4 && url; page++) {
    const pg = await fetchPage(url, src);
    if (pg.error) return { error: pg.error, url };
    if (pg.code >= 400) return { error: `HTTP ${pg.code}`, url };
    const ctx = ctxOf(src, page + 1, pg);
    let raw = pick(rc.content, ctx);
    if (raw && typeof raw === 'object' && raw.tagName) raw = raw.toString();
    let text = '';
    if (Array.isArray(raw)) text = raw.map((x) => cleanText(typeof x === 'string' ? x : (x.tagName ? x.toString() : ''))).join('\n');
    else text = cleanText(raw);
    // replaceRegex: 形如 ##re##repl
    const rr = (rc.replaceRegex || '').trim();
    if (rr.includes('##')) {
      const seg = rr.split('##');
      if (seg[1]) {
        try { text = text.replace(new RegExp(seg[1], 'g'), seg[2] || ''); } catch { /* skip */ }
      }
    }
    if (!title) {
      const t = pick(src.ruleBookInfo && src.ruleBookInfo.name, ctx);
      title = E.str(t);
    }
    if (text) parts.push(text);
    // 下一页
    let next = '';
    const nr = (rc.nextContentUrl || '').trim();
    if (nr) next = E.absUrl(E.str(pick(nr, ctx)), pg.baseUrl);
    if (!next || next === url) break;
    url = next;
  }
  const body = parts.join('\n\n').trim();
  return { title, content: body, chars: body.length, pages: parts.length };
}

/* ---------------- 源可用性测试 ---------------- */
async function cmdTest(job) {
  const src = job.source;
  const t0 = Date.now();
  try {
    const r = await E.doSearch(src, job.key || '剑来', 1);
    if (r.error) return { ok: false, error: r.error, ms: Date.now() - t0 };
    return { ok: r.books.length > 0, count: r.books.length, sample: r.books[0] || null, url: r.url, ms: Date.now() - t0 };
  } catch (e) {
    return { ok: false, error: String(e && e.message || e), ms: Date.now() - t0 };
  }
}

/* ---------------- 主流程 ---------------- */
async function main() {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  const job = JSON.parse(Buffer.concat(chunks).toString('utf-8') || '{}');
  const map = { search: cmdSearch, info: cmdInfo, toc: cmdToc, content: cmdContent, test: cmdTest };
  const fn = map[job.cmd];
  if (!fn) throw new Error('unknown cmd: ' + job.cmd);
  const out = await fn(job);
  process.stdout.write(JSON.stringify(out), () => process.exit(0));
}

main().catch((e) => {
  process.stdout.write(JSON.stringify({ error: String(e && e.message || e) }));
  process.exit(0);
});
