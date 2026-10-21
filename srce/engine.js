'use strict';
/**
 * Legado 书源规则引擎（Node 版）
 * 用法：node engine.js   —— stdin 读一个 JSON 任务，stdout 输出 JSON 结果
 *
 * 支持：jsoup 简化语法 / @css: / JSONPath / 正则 ## ## / 串联 && || %% /
 *      {{JS表达式}} 模板 / <js> 与 @js: 脚本 / java.* 兼容层
 */

const cheerio = require('cheerio');
const { execFileSync } = require('child_process');
const crypto = require('crypto');

const DEFAULT_UA =
  'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36';

/* ------------------------------------------------------------------ *
 *  工具：切分（引号/括号内的分隔符不算）
 * ------------------------------------------------------------------ */
function splitTop(str, sep) {
  const out = [];
  let depth = 0, quote = null, cur = '';
  for (let i = 0; i < str.length; i++) {
    const ch = str[i];
    if (quote) {
      cur += ch;
      if (ch === '\\') { cur += str[++i] || ''; continue; }
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === '`') { quote = ch; cur += ch; continue; }
    if (ch === '(' || ch === '[' || ch === '{') { depth++; cur += ch; continue; }
    if (ch === ')' || ch === ']' || ch === '}') { depth--; cur += ch; continue; }
    if (depth === 0 && str.startsWith(sep, i)) {
      out.push(cur); cur = ''; i += sep.length - 1; continue;
    }
    cur += ch;
  }
  out.push(cur);
  return out;
}

function isBlank(v) {
  if (v === null || v === undefined) return true;
  if (Array.isArray(v)) return v.length === 0;
  return String(v).trim() === '';
}

function flatten(v) {
  if (Array.isArray(v)) return v.flatMap(flatten);
  return [v];
}

function asList(v) {
  if (v === null || v === undefined) return [];
  return Array.isArray(v) ? v : [v];
}

/* ------------------------------------------------------------------ *
 *  HTTP（异步 fetch + 同步 curl 两种）
 * ------------------------------------------------------------------ */
const cookieJar = new Map(); // host -> Map(name -> value)

function jarFor(url) {
  let host = '';
  try { host = new URL(url).host; } catch { return new Map(); }
  if (!cookieJar.has(host)) cookieJar.set(host, new Map());
  return cookieJar.get(host);
}

function cookieHeader(url) {
  const jar = jarFor(url);
  if (!jar.size) return '';
  return [...jar.entries()].map(([k, v]) => `${k}=${v}`).join('; ');
}

function storeCookies(url, headers) {
  const raw = headers.getSetCookie ? headers.getSetCookie() : [];
  if (!raw.length) return;
  const jar = jarFor(url);
  for (const line of raw) {
    const first = line.split(';')[0];
    const eq = first.indexOf('=');
    if (eq > 0) jar.set(first.slice(0, eq).trim(), first.slice(eq + 1).trim());
  }
}

function decodeBody(buf, contentType) {
  const head = Buffer.from(buf.slice(0, 2048)).toString('latin1');
  let cs = (contentType || '').match(/charset=["']?([\w-]+)/i)?.[1];
  if (!cs) cs = head.match(/charset=["']?([\w-]+)/i)?.[1];
  if (!cs) {
    const m = head.match(/<meta[^>]+charset=["']?([\w-]+)/i);
    if (m) cs = m[1];
  }
  cs = (cs || 'utf-8').toLowerCase();
  if (cs === 'gb2312' || cs === 'gbk') cs = 'gb18030';
  try {
    return new TextDecoder(cs).decode(buf);
  } catch {
    return new TextDecoder('utf-8').decode(buf);
  }
}

async function httpGet(url, opts = {}) {
  return httpRequest(url, Object.assign({}, opts, { method: 'GET' }));
}

async function httpRequest(url, opts = {}) {
  const tries = opts.retry === undefined ? 0 : opts.retry;
  let last = null;
  for (let i = 0; i <= tries; i++) {
    last = await httpOnce(url, opts);
    const transient = !last.code || last.code >= 500;
    if (!transient || i === tries) return last;
    await new Promise((r) => setTimeout(r, 600 + i * 900));
  }
  return last;
}

async function httpOnce(url, opts = {}) {
  const headers = Object.assign(
    { 'User-Agent': DEFAULT_UA, Accept: 'text/html,application/json,*/*' },
    opts.headers || {}
  );
  const ck = cookieHeader(url);
  if (ck && !headers.Cookie) headers.Cookie = ck;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), opts.timeout || 9000);
  try {
    const res = await fetch(url, {
      method: opts.method || 'GET',
      headers,
      body: opts.body !== undefined ? opts.body : undefined,
      redirect: 'follow',
      signal: ctrl.signal,
    });
    storeCookies(url, res.headers);
    const buf = Buffer.from(await res.arrayBuffer());
    return {
      code: res.status,
      url: res.url || url,
      rawBody: buf,
      body: decodeBody(buf, res.headers.get('content-type')),
      contentType: res.headers.get('content-type') || '',
    };
  } catch (e) {
    return {
      code: 0,
      error: /abort/i.test(String(e && e.name)) ? '请求超时' : '网络失败: ' + String((e && e.cause && e.cause.code) || (e && e.code) || (e && e.message) || e),
      url,
      rawBody: Buffer.alloc(0),
      body: '',
      contentType: '',
    };
  } finally {
    clearTimeout(timer);
  }
}

/** JS 里 java.ajax 用的同步请求 */
function httpGetSync(url, opts = {}) {
  const headers = Object.assign({ 'User-Agent': DEFAULT_UA }, opts.headers || {});
  const ck = cookieHeader(url);
  if (ck && !headers.Cookie) headers.Cookie = ck;
  const args = ['-sS', '-L', '--compressed', '--max-time', String(Math.round((opts.timeout || 15000) / 1000))];
  for (const [k, v] of Object.entries(headers)) args.push('-H', `${k}: ${v}`);
  args.push(url);
  try {
    const out = execFileSync('curl', args, { maxBuffer: 64 * 1024 * 1024, timeout: 10000 });
    return decodeBody(out, '');
  } catch (e) {
    return '';
  }
}

/* ------------------------------------------------------------------ *
 *  JSONPath 子集
 * ------------------------------------------------------------------ */
function jsonPath(root, path) {
  let p = path.trim();
  if (p.startsWith('@json:')) p = p.slice(6).trim();
  if (!p.startsWith('$')) return undefined;
  p = p.slice(1);
  let cur = [root];
  let i = 0;
  while (i < p.length) {
    if (p.startsWith('..', i)) {
      i += 2;
      let name = '';
      if (p[i] === '*') { name = '*'; i++; }
      else if (p[i] === '[') {
        const end = p.indexOf(']', i);
        name = p.slice(i + 1, end); i = end + 1;
      } else {
        const m = /^[^.\[]+/.exec(p.slice(i));
        name = m ? m[0] : ''; i += name.length;
      }
      const next = [];
      const walk = (node) => {
        if (Array.isArray(node)) { node.forEach(walk); return; }
        if (node && typeof node === 'object') {
          for (const [k, v] of Object.entries(node)) {
            if (name === '*' || k === name) next.push(v);
            walk(v);
          }
        }
      };
      cur.forEach(walk);
      cur = next;
      continue;
    }
    if (p[i] === '.') {
      i++;
      const m = /^[^.\[]+/.exec(p.slice(i));
      if (!m) break;
      const name = m[0]; i += name.length;
      const next = [];
      for (const node of cur) {
        if (node === null || node === undefined) continue;
        if (name === '*') {
          if (Array.isArray(node)) next.push(...node);
          else if (typeof node === 'object') next.push(...Object.values(node));
        } else if (typeof node === 'object' && name in node) next.push(node[name]);
      }
      cur = next;
      continue;
    }
    if (p[i] === '[') {
      const end = p.indexOf(']', i);
      let inner = p.slice(i + 1, end).trim(); i = end + 1;
      const next = [];
      for (const node of cur) {
        if (inner === '*') { asList(node).forEach((x) => next.push(x)); continue; }
        if (/^-?\d+$/.test(inner)) {
          const n = Number(inner);
          if (Array.isArray(node)) { const idx = n < 0 ? node.length + n : n; if (idx in node) next.push(node[idx]); }
          continue;
        }
        if (inner.includes(',')) {
          inner.split(',').forEach((part) => {
            const n = Number(part.trim());
            if (Array.isArray(node) && n in node) next.push(node[n]);
          });
          continue;
        }
        const q = inner.match(/^\?\(@\.([\w.]+)\s*(==|!=|=~|>|<)\s*(.+?)\)$/);
        if (q && Array.isArray(node)) {
          const [, field, op, rawVal] = q;
          const want = rawVal.replace(/^['"]|['"]$/g, '');
          for (const item of node) {
            const got = field.split('.').reduce((o, k) => (o == null ? o : o[k]), item);
            let ok = false;
            if (op === '==') ok = String(got) === want;
            else if (op === '!=') ok = String(got) !== want;
            else if (op === '=~') { try { ok = new RegExp(want).test(String(got)); } catch { ok = false; } }
            else if (op === '>') ok = Number(got) > Number(want);
            else if (op === '<') ok = Number(got) < Number(want);
            if (ok) next.push(item);
          }
          continue;
        }
        // [@attr] 之类忽略
      }
      cur = next;
      continue;
    }
    i++;
  }
  if (cur.length === 0) return undefined;
  if (cur.length === 1) return cur[0];
  return cur;
}

/* ------------------------------------------------------------------ *
 *  JS 执行（带 java.* 兼容层）
 * ------------------------------------------------------------------ */
function absSafe(u, base) {
  if (!u) return base || '';
  try { return new URL(String(u), base || undefined).toString(); } catch { return String(u); }
}

function makeJava(ctx) {
  const base = ctx.baseUrl || '';
  const abs = (u) => {
    if (!u) return u;
    try { return new URL(u, base).toString(); } catch { return u; }
  };
  return {
    ajax: (url, opts) => httpGetSync(abs(String(url)), opts || {}),
    get: (url, opts) => httpGetSync(abs(String(url)), opts || {}),
    post: (url, body, opts) => {
      const headers = Object.assign({ 'Content-Type': 'application/x-www-form-urlencoded' }, (opts && opts.headers) || {});
      const args = ['-sS', '-L', '--compressed', '--max-time', '15', '-X', 'POST', '-d', String(body)];
      for (const [k, v] of Object.entries(headers)) args.push('-H', `${k}: ${v}`);
      args.push(abs(String(url)));
      try { return decodeBody(execFileSync('curl', args, { maxBuffer: 64 * 1024 * 1024, timeout: 10000 }), ''); }
      catch { return ''; }
    },
    base64Encode: (s) => Buffer.from(String(s), 'utf-8').toString('base64'),
    base64Decode: (s) => Buffer.from(String(s), 'base64').toString('utf-8'),
    md5Encode: (s) => crypto.createHash('md5').update(String(s)).digest('hex'),
    md5Encode16: (s) => crypto.createHash('md5').update(String(s)).digest('hex').slice(8, 24),
    timeFormat: (ts) => {
      let t = Number(ts);
      if (!isFinite(t) || t <= 0) return '';
      if (t < 1e11) t *= 1000;
      const d = new Date(t);
      const p = (n) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
    },
    log: () => {},
    logType: () => {},
    toast: () => {},
    put: (k, v) => { ctx.vars[k] = v; return v; },
    getString: (k) => ctx.vars[k],
    getObject: (k) => ctx.vars[k],
    setContent: () => {},
    longToast: () => {},
    startBrowser: () => '',
    startBrowserAwait: () => '',
    connect: () => null,
    androidId: () => '',
    getCookie: (u) => cookieHeader(abs(String(u || base))),
    randomUUID: () => crypto.randomUUID(),
    aesBase64DecodeToString: (data, key, transformation, iv) => {
      try {
        const algo = String(transformation || 'AES/CBC/PKCS5Padding').split('/').slice(0, 2).join('-').toLowerCase();
        const d = crypto.createDecipheriv(algo, Buffer.from(String(key), 'utf-8'), Buffer.from(String(iv || ''), 'utf-8'));
        return Buffer.concat([d.update(Buffer.from(String(data), 'base64')), d.final()]).toString('utf-8');
      } catch (e) { return ''; }
    },
    aesDecodeToString: (data, key, transformation, iv) => {
      try {
        const algo = String(transformation || 'AES/CBC/PKCS5Padding').split('/').slice(0, 2).join('-').toLowerCase();
        const d = crypto.createDecipheriv(algo, Buffer.from(String(key), 'utf-8'), Buffer.from(String(iv || ''), 'utf-8'));
        return Buffer.concat([d.update(Buffer.from(String(data), 'base64')), d.final()]).toString('utf-8');
      } catch (e) { return ''; }
    },
    base64DecodeToString: (s) => Buffer.from(String(s), 'base64').toString('utf-8'),
    base64EncodeToString: (s) => Buffer.from(String(s), 'utf-8').toString('base64'),
    hexDecodeToString: (s) => Buffer.from(String(s), 'hex').toString('utf-8'),
    digestHex: (s) => crypto.createHash('sha256').update(String(s)).digest('hex'),
    htmlFormat: (s) => String(s),
    encodeURI: (s) => encodeURI(String(s)),
  };
}

/** 补上 Legado 里 source.getKey() 等常用方法 */
function ensureSource(src) {
  if (!src || typeof src !== 'object') return src;
  if (!src.getKey) {
    src.getKey = () => crypto.createHash('md5').update(String(src.bookSourceUrl || '')).digest('hex');
  }
  if (!src.getVariable) src.getVariable = () => '';
  return src;
}

function evalJS(code, ctx) {
  const java = makeJava(ctx);
  const vars = Object.assign({}, ctx.vars || {});
  const names = [
    'java', 'result', 'baseUrl', 'key', 'page', 'book',
    'source', 'title', 'src', 'nextChapterUrl', 'baseURL',
  ];
  const values = [
    java, ctx.result, ctx.baseUrl || '', ctx.key || '', ctx.page || 1, ctx.book || null,
    ctx.source || null, ctx.title || '', ctx.baseUrl || '',
    ctx.nextChapterUrl || '', ctx.baseUrl || '',
  ];
  // 让 JS 里能写 $.xxx：把 $ 绑成 JSONPath 代理
  const dollarProxy = new Proxy({}, {
    get: (_t, prop) => {
      if (prop === 'toString') return () => '';
      if (prop === Symbol.toPrimitive) return () => '';
      return jsonPath(ctx.result, '$.' + String(prop));
    },
  });
  names.push('$', 'cookie');
  values.push(dollarProxy, {
    removeCookie: () => '',
    getCookie: (u) => cookieHeader(absSafe(u, ctx.baseUrl)),
    setCookie: () => '',
    replaceCookie: () => '',
  });
  for (const [k, v] of Object.entries(vars)) { names.push(k); values.push(v); }
  let fn;
  try {
    fn = new Function(...names, `"use strict"; return (${code});`);
  } catch {
    fn = new Function(...names, `"use strict"; ${code}`);
  }
  return fn(...values);
}

function renderTemplate(str, ctx) {
  if (!str || !str.includes('{{')) return str;
  return str.replace(/\{\{([\s\S]*?)\}\}/g, (_m, expr) => {
    let code = expr.trim();
    let val;
    try {
      if (code.startsWith('$')) val = jsonPath(ctx.result, code);
      else val = evalJS(code, ctx);
    } catch (e) {
      val = '';
    }
    if (val === null || val === undefined) return '';
    if (typeof val === 'object') return JSON.stringify(val);
    return String(val);
  });
}

/* ------------------------------------------------------------------ *
 *  HTML 规则：jsoup 简化语法 → cheerio
 * ------------------------------------------------------------------ */
function splitIndexSuffix(step) {
  // 支持 tag.N / tag!N / tag[-1] / tag[0:5] / tag[!0]
  let m = step.match(/^(.*?)\[([^\]]*)\]$/);
  if (m && !step.startsWith('@')) {
    const [, base, spec] = m;
    if (/^!?[-\d:,\s]+$/.test(spec)) return { base, spec: spec.trim(), bracket: true };
  }
  m = step.match(/^(.*?)([.!:])(-?\d+(?::-?\d+)*)$/);
  if (m) return { base: m[1], spec: `${m[2] === '!' ? '!' : ''}${m[3]}`, bracket: false };
  return { base: step, spec: null };
}

function applyIndex(nodes, spec) {
  if (!spec) return nodes;
  const exclude = spec.startsWith('!');
  const body = exclude ? spec.slice(1) : spec;
  let picked = [];
  if (body.includes(':')) {
    const parts = body.split(':').map((x) => (x === '' ? null : Number(x)));
    const [a, b, c] = [parts[0] ?? 0, parts[1] ?? null, parts[2] ?? 1];
    const len = nodes.length;
    const start = a < 0 ? len + a : a;
    const end = b === null ? len : (b < 0 ? len + b : b);
    for (let i = start; i < end && i < len; i += c) if (i >= 0) picked.push(i);
  } else if (body.includes(',')) {
    body.split(',').forEach((x) => {
      const n = Number(x.trim());
      const i = n < 0 ? nodes.length + n : n;
      if (i >= 0 && i < nodes.length) picked.push(i);
    });
  } else {
    const n = Number(body);
    const i = n < 0 ? nodes.length + n : n;
    if (i >= 0 && i < nodes.length) picked.push(i);
  }
  if (exclude) return nodes.filter((_, i) => !picked.includes(i));
  return picked.map((i) => nodes[i]);
}

function toSelector(step) {
  let s = step.trim();
  if (s.startsWith('@css:')) s = s.slice(5).trim();
  if (/^class\./.test(s)) return '.' + s.slice(6);
  if (/^class=/.test(s)) return '.' + s.slice(6);
  if (/^id\./.test(s)) return '#' + s.slice(3);
  if (/^id=/.test(s)) return '#' + s.slice(3);
  if (s.startsWith('tag.')) return s.slice(4);
  if (s.startsWith('tag=')) return s.slice(4);
  if (s.startsWith('.')) return s;
  if (s.startsWith('#')) return s;
  return s;
}

const TEXT_STEPS = new Set(['text', 'textNodes', 'ownText', 'html', 'outerHtml', 'all', 'innerHtml']);

function nodesToValue(nodes, step, baseUrl, $) {
  const stepName = String(step).replace(/^@/, '').trim();
  const one = (el, fn) => {
    if (typeof el === 'string') return el;
    try { return fn($(el)); } catch { return ''; }
  };
  if (stepName === 'text' || stepName === 'ownText' || stepName === 'all') {
    return nodes.map((el) => String(one(el, ($e) => $e.text())).trim());
  }
  if (stepName === 'textNodes') {
    return nodes.map((el) => String(one(el, ($e) => $e.text())));
  }
  if (stepName === 'html' || stepName === 'innerHtml') {
    return nodes.map((el) => String(one(el, ($e) => $e.html() || '')));
  }
  if (stepName === 'outerHtml') {
    return nodes.map((el) => String(one(el, ($e) => $.html($e) || '')));
  }
  const attr = stepName;
  const out = [];
  for (const el of nodes) {
    if (typeof el === 'string') { out.push(el); continue; }
    let v;
    try { v = $(el).attr(attr); } catch { v = undefined; }
    if (v === undefined && attr === 'src') {
      try { v = $(el).attr('data-src') || $(el).attr('data-original') || $(el).attr('data-lazy-src'); } catch { /* skip */ }
    }
    if (v === undefined || v === null) continue;
    if ((attr === 'href' || attr === 'src') && v && !/^[a-zA-Z][\w+.-]*:/.test(v)) {
      v = absSafe(v, baseUrl || 'http://localhost');
    }
    out.push(v);
  }
  return out;
}

function applyRegex(val, rule) {
  // rule: ##regex##replacement##  （replacement 可用 $1 分组）
  const parts = splitTop(rule, '##');
  if (parts.length < 2) return val;
  const pattern = parts[1];
  const repl = parts[2] !== undefined ? parts[2] : '';
  if (!pattern) return val;
  let re;
  try { re = new RegExp(pattern, 'g'); } catch { return val; }
  const one = (s) => String(s).replace(re, repl.replace(/\\n/g, '\n'));
  return Array.isArray(val) ? val.map(one) : one(val);
}

/* 主入口：对某段内容执行一条规则 */
function evalRule(ruleRaw, input, ctx) {
  if (ruleRaw === null || ruleRaw === undefined) return '';
  let rule = String(ruleRaw).trim();
  if (!rule) return '';

  // 串联：|| 取第一个非空；&& 合并；%% 交替
  const orParts = splitTop(rule, '||');
  if (orParts.length > 1) {
    for (const part of orParts) {
      const v = evalRule(part, input, ctx);
      if (!isBlank(v)) return v;
    }
    return '';
  }
  const andParts = splitTop(rule, '&&');
  if (andParts.length > 1) {
    const all = [];
    for (const part of andParts) all.push(...asList(evalRule(part, input, ctx)));
    return all;
  }
  const interParts = splitTop(rule, '%%');
  if (interParts.length > 1) {
    const lists = interParts.map((p) => asList(evalRule(p, input, ctx)));
    const out = [];
    const maxLen = Math.max(...lists.map((l) => l.length), 0);
    for (let i = 0; i < maxLen; i++) for (const l of lists) if (i < l.length) out.push(l[i]);
    return out;
  }

  // @put:{...} 只是存变量，取值时忽略
  rule = rule.replace(/@put:\{[\s\S]*?\}/g, '').trim();

  // JS 规则
  let jsCode = null;
  const jsTag = rule.match(/<js>([\s\S]*?)<\/js>/);
  if (jsTag) { jsCode = jsTag[1]; rule = rule.replace(jsTag[0], '').trim(); }
  else if (rule.startsWith('@js:')) { jsCode = rule.slice(4); rule = ''; }

  // 正则部分（可能出现在规则尾部）；HTML 规则交给 evalHtmlRule 按步处理
  const htmlRule = !!ctx.doc && !/^\$|^@json:/.test(rule) && !rule.includes('{{');
  let regexPart = '';
  if (!htmlRule && rule.includes('##')) {
    const idx = rule.indexOf('##');
    regexPart = rule.slice(idx);
    rule = rule.slice(0, idx).trim();
  }

  let val;
  const jsonMode = !ctx.doc || (ctx.result !== null && typeof ctx.result === 'object');
  if (rule.includes('{{') && !rule.includes('@')) {
    // 整条规则就是模板，例如 /novel/{{$.novelId}}?isSearch=1
    val = renderTemplate(rule, ctx);
  } else if (!rule) {
    val = ctx.result;
  } else if (rule.startsWith('$') || rule.startsWith('@json:')) {
    val = jsonPath(ctx.result, rule);
  } else if (jsonMode && /^[\w$.*\[\]\-]+$/.test(rule) && !/^(text|html|outerHtml|textNodes|ownText|all)$/.test(rule)) {
    // JSON 源常见写法：body.books / bookName（省略 $.）
    val = jsonPath(ctx.result, '$.' + rule.replace(/^\./, ''));
  } else if (ctx.doc) {
    val = evalHtmlRule(rule, ctx);
  } else {
    val = '';
  }

  if (regexPart) val = applyRegex(val, regexPart);

  if (jsCode !== null) {
    const sub = Object.assign({}, ctx, { result: val });
    try { val = evalJS(jsCode, sub); } catch (e) { val = ''; }
  }
  return val;
}

const TAG_NAMES = new Set(('a abbr address article aside audio b bdi bdo blockquote body br button canvas caption ' +
  'center cite code col colgroup dd del details dfn dialog div dl dt em fieldset figcaption figure font footer ' +
  'form h1 h2 h3 h4 h5 h6 head header hr html i iframe img input ins kbd label legend li link main map mark ' +
  'marquee meta nav noscript object ol optgroup option p picture pre q rp rt ruby s samp script section select ' +
  'slot small source span strong style sub summary sup table tbody td template textarea tfoot th thead time ' +
  'title tr track u ul var video wbr').split(/\s+/));

function applyRegexSpec(val, spec) {
  const seg = String(spec).split('##');
  const pat = seg[0];
  const rep = seg.length > 1 ? seg[1] : '';
  if (!pat) return val;
  let re;
  try { re = new RegExp(pat, 'g'); } catch { return val; }
  const one = (s) => String(s == null ? '' : s).replace(re, rep);
  return Array.isArray(val) ? val.map(one) : one(val);
}

/** 末步是否当作「取属性」而不是选择器 */
function isAttrName(tok) {
  const t = String(tok).trim();
  if (!t) return false;
  if (TEXT_STEPS.has(t)) return true;
  if (/^(href|src|onclick|value|title|alt|content|datetime|data-[\w-]+|action|poster|datetime)$/i.test(t)) return true;
  if (/^[a-zA-Z][\w:-]*$/.test(t) && !TAG_NAMES.has(t.toLowerCase())) return true;
  return false;
}

function evalHtmlRule(rule, ctx) {
  const $ = ctx.doc;
  const steps = splitTop(rule, '@');
  let nodes = null;
  let val = null;

  const scopedFind = (sel, fromNodes) => {
    const found = [];
    const bases = fromNodes || (ctx.scope && ctx.scope.length ? ctx.scope : null);
    if (!bases) {
      try { return $(sel).toArray(); } catch { return []; }
    }
    for (const el of bases) {
      if (typeof el === 'string') continue;
      try {
        if ($(el).is(sel)) found.push(el);
        $(el).find(sel).each((_j, n) => found.push(n));
      } catch { /* skip */ }
    }
    return found;
  };

  const currentValue = () => {
    if (val !== null) return val;
    if (nodes && nodes.length) return nodesToValue(nodes, 'text', ctx.baseUrl, $);
    if (ctx.scope && ctx.scope.length) return nodesToValue(ctx.scope, 'text', ctx.baseUrl, $);
    return ctx.result;
  };

  for (let i = 0; i < steps.length; i++) {
    let raw = steps[i].trim();
    if (!raw) continue;
    let regexSpec = null;
    const hashAt = raw.indexOf('##');
    if (hashAt >= 0) { regexSpec = raw.slice(hashAt + 2); raw = raw.slice(0, hashAt).trim(); }
    const isLast = i === steps.length - 1;
    const fromAt = i > 0 && steps[i - 1].trim() === '';   // 规则以 @attr 开头

    // 内联 JS（@js: 被 split 成 js:）
    if (/^js:/.test(raw)) {
      const code = raw.slice(3);
      let cur = currentValue();
      if (Array.isArray(cur) && cur.length === 1) cur = cur[0];
      try {
        val = evalJS(code, Object.assign({}, ctx, { result: cur }));
      } catch { return ''; }
      if (regexSpec) val = applyRegexSpec(val, regexSpec);
      nodes = null;
      continue;
    }

    // 子级 CSS
    if (/^css:/.test(raw)) {
      const sel = raw.slice(4).trim();
      const next = scopedFind(sel, nodes);
      nodes = next.length ? next : (nodes || (ctx.scope && ctx.scope.length ? ctx.scope : null));
      if (!nodes) return '';
      continue;
    }

    // 取值步骤：@text / @href / text / 末步属性名
    if (fromAt || TEXT_STEPS.has(raw) || (isLast && isAttrName(raw))) {
      const list = nodes || (ctx.scope && ctx.scope.length ? ctx.scope : null);
      if (!list) return '';
      let v = nodesToValue(list, raw, ctx.baseUrl, $);
      if (regexSpec) v = applyRegexSpec(v, regexSpec);
      if (isLast) return v;
      val = v;
      nodes = null;
      continue;
    }

    // 选择器
    const { base, spec } = splitIndexSuffix(raw);
    const sel = toSelector(base);
    let found = scopedFind(sel, nodes);
    if (spec) found = applyIndex(found, spec);
    nodes = found;
    val = null;
    if (!nodes.length) return '';
  }
  if (val !== null) return val;
  if (!nodes) return '';
  if (nodes.length === 1) return nodes[0];
  return nodes;
}

/* ------------------------------------------------------------------ *
 *  规则集执行（search / bookInfo / toc / content）
 * ------------------------------------------------------------------ */
function parseHeader(src) {
  const h = src.header;
  if (!h) return {};
  try {
    const obj = typeof h === 'string' ? JSON.parse(h) : h;
    const out = {};
    for (const [k, v] of Object.entries(obj || {})) {
      if (k.toLowerCase() === 'cookie') continue; // cookie 单独处理
      out[k] = String(v);
    }
    const ck = obj && (obj.Cookie || obj.cookie);
    if (ck) out.Cookie = String(ck);
    return out;
  } catch { return {}; }
}

function absUrl(u, baseUrl) {
  if (!u) return '';
  const s = String(u).trim();
  if (!s) return '';
  if (/^[a-zA-Z][\w+.-]*:/.test(s)) return s;
  try { return new URL(s, baseUrl).toString(); } catch { return s; }
}

function str(v) {
  if (v === null || v === undefined) return '';
  if (Array.isArray(v)) return v.filter((x) => x !== null && x !== undefined).map(str).join(', ');
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v).replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
}

function buildSearchUrl(src, key, page) {
  const raw = (src.searchUrl || '').trim();
  if (!raw) return '';
  const ctx = { result: null, baseUrl: src.bookSourceUrl, key, page: page || 1, vars: {}, source: src };
  const { url, options } = parseUrlRule(raw, ctx);
  return absUrl(url, src.bookSourceUrl);
}

/** 解析 URL 规则：去掉前置 {{语句}} 行、拆出尾部 ,{options}、渲染模板 */
function parseUrlRule(raw, ctx) {
  let s = String(raw || '').trim();
  let options = {};

  // 尾部 ,{json}
  const m = s.match(/,\s*(\{[\s\S]*\})\s*$/);
  if (m) {
    try { options = JSON.parse(m[1]); s = s.slice(0, m.index).trim(); }
    catch {
      try {
        // 单引号对象字面量：{'method':'POST'}
        options = new Function('return (' + m[1] + ');')() || {};
        s = s.slice(0, m.index).trim();
      } catch { /* 不是选项 */ }
    }
  }

  // 首行可能是 {{cookie.removeCookie(...)}} 这类语句
  const lines = s.split('\n').map((x) => x.trim()).filter(Boolean);
  while (lines.length > 1 && /^\{\{[\s\S]*\}\}$/.test(lines[0])) {
    try { renderTemplate(lines[0], ctx); } catch { /* 忽略副作用 */ }
    lines.shift();
  }
  s = lines.join('\n').trim();

  // @js: 形式的 URL 规则
  if (s.startsWith('@js:') || s.startsWith('<js>')) {
    const code = s.startsWith('@js:') ? s.slice(4) : s.replace(/<\/?js>/g, '');
    try {
      const v = evalJS(code, ctx);
      if (v && typeof v === 'object') {
        return {
          url: String(v.url || v.URL || ''),
          options: Object.assign({}, options, v),
        };
      }
      return { url: String(v == null ? '' : v), options };
    } catch {
      return { url: '', options };
    }
  }

  // URL 上下文里 {{key}} 需要 URL 编码
  if (ctx.key !== undefined && ctx.key !== null && ctx.key !== '') {
    s = s.replace(/\{\{\s*key\s*\}\}/g, encodeURIComponent(String(ctx.key)));
  }
  s = renderTemplate(s, ctx).trim();
  return { url: s, options };
}

/** 按规则取页面（支持 GET/POST、charset、额外 headers） */
async function fetchRule(rawRule, ctx, src, extraHeaders) {
  const { url: rawUrl, options } = parseUrlRule(rawRule, ctx);
  const url = absUrl(rawUrl, ctx.baseUrl || src.bookSourceUrl);
  if (!url) return { error: '空URL', url: '' };
  const headers = Object.assign({}, parseHeader(src), extraHeaders || {}, options.headers || {});
  const method = String(options.method || 'GET').toUpperCase();
  const timeout = Number(options.timeout) ? Number(options.timeout) * 1000 : 15000;
  let res;
  if (method === 'POST') {
    let body = options.body === undefined ? '' : renderTemplate(String(options.body), ctx);
    if (options.charset && /gbk|gb2312/i.test(options.charset)) {
      body = Buffer.from(body, 'utf-8').toString('binary');
    }
    if (!headers['Content-Type'] && !headers['content-type']) {
      headers['Content-Type'] = 'application/x-www-form-urlencoded';
    }
    res = await httpRequest(url, { method: 'POST', body, headers, timeout });
  } else {
    res = await httpGet(url, { headers, timeout });
  }
  if (options.charset && /gbk|gb2312/i.test(options.charset)) {
    res.body = new TextDecoder('gb18030').decode(Buffer.from(res.rawBody || []));
  }

  // 跟随 JS / meta 跳转（很多小说站的反爬跳转页）
  for (let hop = 0; hop < 2; hop++) {
    const body = String(res.body || '');
    if (body.length > 4000) break;
    let target = '';
    const js = body.match(/window\.location(?:\.href)?\s*=\s*["']([^"']+)["']/);
    const meta = body.match(/http-equiv=["']?refresh["']?[^>]*content=["'][^"']*url=([^"';]+)/i);
    if (js) target = js[1];
    else if (meta) target = meta[1];
    if (!target) break;
    const nextUrl = absUrl(target, res.url || url);
    if (!nextUrl || nextUrl === (res.url || url)) break;
    res = await httpRequest(nextUrl, { method: 'GET', headers, timeout });
    if (options.charset && /gbk|gb2312/i.test(options.charset)) {
      res.body = new TextDecoder('gb18030').decode(Buffer.from(res.rawBody || []));
    }
  }

  res.options = options;
  return res;
}

async function doSearch(src, key, page) {
  ensureSource(src);
  const ctx0 = { result: null, baseUrl: src.bookSourceUrl, key, page: page || 1, vars: {}, source: src };
  const res = await fetchRule(src.searchUrl, ctx0, src);
  if (res.error) return { error: res.error };
  const url = res.url;
  if (res.code >= 400) return { error: `HTTP ${res.code}`, url };
  const body = res.body;
  const isJson = /^\s*[\[{]/.test(body) || /json/i.test(res.contentType);
  let doc = null, result = body;
  if (isJson) {
    try { result = JSON.parse(body); } catch { result = body; }
  } else {
    doc = cheerio.load(body);
  }
  const ctx = { doc, result, baseUrl: res.url || url, key, page: page || 1, source: src, vars: {} };
  const rs = src.ruleSearch || {};
  const listRule = (rs.bookList || '').trim();

  let items;
  if (listRule) {
    const raw = evalRule(listRule, body, ctx);
    if (typeof raw === 'string' && doc) {
      // bookList 返回选择器字符串？按选择器处理
      try { items = doc(raw).toArray(); } catch { items = []; }
    } else {
      items = asList(raw);
    }
  } else {
    items = [ctx.result];
  }

  const books = [];
  for (const item of items.slice(0, 40)) {
    if (item === null || item === undefined) continue;
    const isEl = item && typeof item === 'object' && item.tagName;
    const isObj = item && typeof item === 'object' && !isEl;
    const ictx = {
      doc: isObj ? null : doc,
      scope: isEl ? [item] : null,
      result: isEl ? item.toString() : item,
      baseUrl: ctx.baseUrl,
      key, page: page || 1, source: src, vars: {},
    };
    const get = (k) => {
      const r = (rs[k] || '').trim();
      if (!r) return '';
      try { return evalRule(r, ictx.result, ictx); } catch { return ''; }
    };
    const name = str(get('name'));
    if (!name) continue;
    books.push({
      name,
      author: str(get('author')),
      kind: str(get('kind')),
      intro: str(get('intro')),
      coverUrl: absUrl(str(get('coverUrl')), ictx.baseUrl),
      bookUrl: absUrl(str(get('bookUrl')), ictx.baseUrl),
      lastChapter: str(get('lastChapter')),
      wordCount: str(get('wordCount')),
      origin: src.bookSourceUrl,
      originName: src.bookSourceName,
    });
  }
  return { url, books };
}

/** 把单个元素包成可查询的 doc */
function makeElemDoc(el) {
  const $ = cheerio.load(el.toString());
  const root = $.root();
  // 让 $('sel') 在整棵树里查，同时 @text 落在根元素上
  return {
    root: () => root,
    __el: el,
    sel: (s) => {
      const $2 = cheerio.load(el.toString());
      return $2(s);
    },
    call: null,
  };
}

module.exports = {
  evalRule, jsonPath, renderTemplate, evalJS, httpGet, httpGetSync, httpRequest,
  fetchRule, parseUrlRule, ensureSource,
  doSearch, absUrl, str, buildSearchUrl, parseHeader, cheerio, splitTop, asList, isBlank,
};
