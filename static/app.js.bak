/* 网页版阅读器 —— 前端逻辑（无框架） */
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const DEFAULTS = {
  fontSize: 19, lineHeight: 1.85, padding: 18, font: 'system', theme: 'dark',
  pageMode: 'scroll',              // scroll 滚动 | page 整页 | slide 左右
  autoCh: true,                    // 滚动到底自动载入下一章
  tts: {
    engine: 'http', url: '', method: 'POST', headers: '', body: '',
    voice: '', speed: 1.18, proxy: 'http://127.0.0.1:7890', autoNext: true
  }
};

let S = JSON.parse(JSON.stringify(DEFAULTS));
let book = null, chIdx = 0, chCount = 0, sentences = [], spans = [], units = [];
let previewing = false;
let chunks = [];            // 已拼接进正文的章节块（连续阅读）
let prefetched = new Map(); // idx -> 已取回的章节数据
let appending = false, endMarked = false;
let presets = {};
let saveTimer = null;

/* ---------------- 基础工具 ---------------- */
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const ct = r.headers.get('content-type') || '';
  if (ct.includes('application/json')) {
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    return j;
  }
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r;
}
function toast(msg, kind = '', ms = 2600) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast ' + kind;
  clearTimeout(t._t);
  t._t = setTimeout(() => t.classList.add('hide'), ms);
}
function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}

/* ---------------- 设置 ---------------- */
async function loadSettings() {
  try {
    const s = await api('/api/settings');
    if (s && Object.keys(s).length) {
      S = { ...DEFAULTS, ...s, tts: { ...DEFAULTS.tts, ...(s.tts || {}) } };
    }
  } catch (e) { console.warn('读设置失败', e); }
  applyStyle();
}
async function saveSettings() {
  try { await api('/api/settings', { method: 'PUT', body: JSON.stringify(S) }); }
  catch (e) { toast('保存失败：' + e.message, 'err'); }
}
function applyStyle() {
  const r = document.documentElement.style;
  r.setProperty('--fs', S.fontSize + 'px');
  r.setProperty('--lh', S.lineHeight);
  r.setProperty('--pd', S.padding + 'px');
  document.body.dataset.theme = S.theme;
  document.body.dataset.font = S.font;
  if (S.pageMode === 'slide') applyPageMode();   // 字号/边距变了，列宽要重算
}

/* ---------------- 书架 ---------------- */
async function loadShelf() {
  const { books } = await api('/api/books');
  const grid = $('#grid');
  grid.innerHTML = '';
  $('#shelfEmpty').classList.toggle('hide', books.length > 0);
  for (const b of books) {
    const pct = b.ch_count ? Math.min(100, Math.round((b.progress_ch + b.progress_off) / b.ch_count * 100)) : 0;
    const hue = (b.seed * 37) % 360;
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <div class="cover" style="background:linear-gradient(150deg,hsl(${hue} 46% 38%),hsl(${(hue + 42) % 360} 52% 22%))">
        <span>${escapeHtml(b.title)}</span>
      </div>
      <div class="name">${escapeHtml(b.title)}</div>
      <div class="meta">${b.ch_count} 章 · ${pct}%</div>
      <button class="del" title="删除">✕</button>`;
    card.querySelector('.del').onclick = async (ev) => {
      ev.stopPropagation();
      if (!confirm(`删除《${b.title}》？`)) return;
      await api('/api/books/' + b.id, { method: 'DELETE' });
      toast('已删除');
      loadShelf();
    };
    card.onclick = () => openBook(b.id);
    grid.appendChild(card);
  }
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ---------------- 导入 ---------------- */
async function importFiles(files) {
  for (const f of files) {
    toast(`正在导入 ${f.name} …`, '', 60000);
    try {
      const r = await api('/api/import?filename=' + encodeURIComponent(f.name), {
        method: 'POST', body: f
      });
      toast(`《${r.title}》导入成功，${r.chapters} 章`, 'ok');
    } catch (e) {
      toast(`《${f.name}》导入失败：${e.message}`, 'err', 5000);
    }
  }
  loadShelf();
}

/* ---------------- 阅读 ---------------- */
async function openBook(id) {
  book = await api('/api/books/' + id);
  chIdx = Math.min(book.progress_ch || 0, Math.max(0, book.chapters.length - 1));
  chCount = book.chapters.length;
  $('#bookTitle').textContent = book.title;
  $('#shelf').classList.add('hide');
  $('#reader').classList.remove('hide');
  renderToc();
  await loadChapter(chIdx, book.progress_off || 0);
}
async function loadChapter(i, off = 0, keepSpeak = false) {
  if (i < 0 || i >= chCount) return false;
  const wasSpeaking = keepSpeak || speaking;
  stopSpeak();
  let d;
  try {
    d = await fetchCh(i);
  } catch (e) {
    toast('章节加载失败：' + e.message, 'err', 5000);
    setHint('章节加载失败');
    return false;
  }
  chIdx = i;
  chCount = d.count || chCount;
  previewing = false;
  resetContent();
  appendChunk(i, d);
  requestAnimationFrame(() => {
    setScroll(off);
    applyPageMode();
    updateProgress();
    ensureAhead();
  });
  markTocCurrent();
  prefetchAhead();
  if (wasSpeaking) {
    speaking = true;
    $('#ttsPlay').textContent = '⏸';
    startFrom(chunks.length ? chunks[0].baseUnit : 0);
  }
  return true;
}
function splitSentences(t) {
  const ENDS = '。！？!?…；;';
  const parts = [];
  let buf = '';
  for (const ch of t) {
    buf += ch;
    if (ENDS.includes(ch)) { if (buf.trim()) parts.push(buf.trim()); buf = ''; }
  }
  if (buf.trim()) parts.push(buf.trim());
  const out = [];
  for (const s of parts) {
    if (s.length <= 120) { out.push(s); continue; }
    let rest = s;
    while (rest.length > 120) {
      let cut = Math.max(rest.lastIndexOf('，', 120), rest.lastIndexOf('、', 120), rest.lastIndexOf('：', 120));
      if (cut < 40) cut = 120;
      out.push(rest.slice(0, cut + 1));
      rest = rest.slice(cut + 1);
    }
    if (rest.trim()) out.push(rest);
  }
  return out;
}

/* ---------- 朗读单元：纯标点不送 TTS，短句合并 ---------- */
const SPEAK_RE = /[0-9A-Za-z\u00c0-\u024f\u0370-\u03ff\u0400-\u04ff\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]/;
const UNIT_MAX = 90;
function speakable(t) { return SPEAK_RE.test(t || ''); }
function cleanForTts(t) {
  return String(t || '')
    .replace(/[\u200b-\u200f\u202a-\u202e\ufeff]/g, '')
    .replace(/[.．]{3,}/g, '…')
    .replace(/…{2,}/g, '…')
    .replace(/[—–―]{2,}/g, '——')
    .replace(/\s+/g, ' ')
    .trim();
}
/* 把段落切成句子，再合并成朗读单元（纯标点句只并入，不单独成单元） */
function computeUnits(paras) {
  const flat = [], us = [];
  let buf = '', from = -1, to = -1;
  const flush = () => {
    if (from >= 0) {
      const text = cleanForTts(buf);
      if (speakable(text)) us.push({ text, from, to });
    }
    buf = ''; from = -1; to = -1;
  };
  for (const para of paras) {
    for (const s of splitSentences(para)) {
      const idx = flat.length;
      flat.push(s);
      if (speakable(s)) {
        if (from < 0) from = idx;
        to = idx;
        buf += s;
        if (buf.length >= UNIT_MAX) flush();
      } else if (from >= 0) {
        buf += s;          // 纯标点句并进当前单元，绝不单独送模型
        to = idx;
      }
    }
    flush();
  }
  return { flat, units: us };
}
function resetContent() {
  sentences = []; spans = []; units = []; chunks = [];
  curIdx = -1; endMarked = false;
  $('#content').innerHTML = '';
}
/* 把一章追加到正文末尾：只往后加，不动滚动位置，所以跨章看不出接缝 */
function appendChunk(i, d) {
  const c = $('#content');
  const wrap = document.createElement('section');
  wrap.className = 'ch-block';
  wrap.dataset.ch = i;
  const h = document.createElement('h2');
  h.className = 'ch-head' + (chunks.length ? '' : ' first');
  h.textContent = d.title || `第 ${i + 1} 章`;
  wrap.appendChild(h);
  const baseSpan = spans.length, baseUnit = units.length;
  let k = baseSpan;
  const paras = d.paras || [];
  for (const para of paras) {
    const p = document.createElement('p');
    for (const s of splitSentences(para)) {
      const sp = document.createElement('span');
      sp.className = 's';
      sp.dataset.i = k;
      sp.textContent = s;
      p.appendChild(sp);
      spans.push(sp);
      k++;
    }
    if (p.childNodes.length) wrap.appendChild(p);
  }
  const r = computeUnits(paras);
  for (const u of r.units) units.push({ text: u.text, from: baseSpan + u.from, to: baseSpan + u.to, ch: i });
  c.appendChild(wrap);
  const chunk = { idx: i, title: d.title, el: wrap, baseSpan, baseUnit, endUnit: units.length - 1 };
  chunks.push(chunk);
  endMarked = false;
  return chunk;
}
/* ---------- 无缝续章：快读到底时静默把下一章拼到后面 ---------- */
function setTtsPanel(open) {
  $('#ttsPanel').classList.toggle('hide', !open);
  document.body.classList.toggle('tts-open', !!open);
  if (open) requestAnimationFrame(() => ensureAhead());
}
function readerOpen() { return !$('#reader').classList.contains('hide'); }

async function fetchCh(i) {
  if (prefetched.has(i)) { const d = prefetched.get(i); prefetched.delete(i); return d; }
  return api(`/api/books/${book.id}/ch/${i}`);
}
/* 提前把下一章正文取回内存，真要用时不再等网络 */
function prefetchAhead() {
  if (!book || book.id < 0 || previewing) return;
  const last = chunks[chunks.length - 1];
  if (!last) return;
  const n = last.idx + 1;
  if (n >= chCount || prefetched.has(n)) return;
  api(`/api/books/${book.id}/ch/${n}`).then((d) => prefetched.set(n, d)).catch(() => { });
}
/* 剩余可滚距离不足 2.5 屏 → 该往后铺了 */
function needMore() {
  const c = $('#content');
  const slide = S.pageMode === 'slide';
  const pos = slide ? c.scrollLeft : c.scrollTop;
  const total = slide ? c.scrollWidth : c.scrollHeight;
  const view = (slide ? c.clientWidth : c.clientHeight) || 1;
  return total - (pos + view) < view * 2.5;
}
async function appendNext(silent = true) {
  if (appending || previewing || !book || !chunks.length) return false;
  const i = chunks[chunks.length - 1].idx + 1;
  if (i >= chCount) { markBookEnd(); return false; }
  appending = true;
  try {
    const d = await fetchCh(i);
    if (previewing || !chunks.length || chunks[chunks.length - 1].idx + 1 !== i) return false;
    chCount = d.count || chCount;
    const ch = appendChunk(i, d);
    if (S.pageMode !== 'scroll') requestAnimationFrame(applyPageMode);
    // 朗读中：顺手把下一章第一句合成好，跨章不留空档
    if (speaking && S.tts.engine !== 'web' && units[ch.baseUnit]) getAudio(units[ch.baseUnit].text).catch(() => { });
    prefetchAhead();
    return true;
  } catch (e) {
    if (!silent) toast('下一章加载失败：' + e.message, 'err', 4000);
    return false;
  } finally {
    appending = false;
  }
}
function ensureAhead() {
  if (previewing || !chunks.length || !readerOpen()) return;
  trimChunks();
  if (S.autoCh === false) return;
  if (needMore()) appendNext();
}
/* 长读时丢掉已经远远在头顶上的章节块，避免 DOM 无限膨胀（视觉上无变化） */
function trimChunks() {
  if (S.pageMode === 'slide' || chunks.length <= 16) return;
  const c = $('#content');
  const cr = c.getBoundingClientRect();
  while (chunks.length > 12) {
    const first = chunks[0], second = chunks[1];
    if (first.el.getBoundingClientRect().bottom > cr.top - cr.height * 3) break;
    const anchor = second.el.querySelector('.ch-head');
    const before = anchor ? anchor.getBoundingClientRect().top : 0;
    const nSpans = second.baseSpan, nUnits = second.baseUnit;
    first.el.remove();
    chunks.shift();
    spans.splice(0, nSpans);
    spans.forEach((s, k) => { s.dataset.i = k; });
    units.splice(0, nUnits);
    units.forEach((u) => { u.from -= nSpans; u.to -= nSpans; });
    for (const ch of chunks) { ch.baseSpan -= nSpans; ch.baseUnit -= nUnits; ch.endUnit -= nUnits; }
    if (curIdx >= 0) curIdx = Math.max(0, curIdx - nUnits);
    const after = anchor ? anchor.getBoundingClientRect().top : 0;
    const prev = c.style.scrollBehavior;
    c.style.scrollBehavior = 'auto';
    c.scrollTop += after - before;
    void c.offsetHeight;
    c.style.scrollBehavior = prev;
  }
}
function markBookEnd() {
  if (endMarked || previewing || !chunks.length) return;
  endMarked = true;
  const e = document.createElement('div');
  e.className = 'chend';
  e.innerHTML = '<div class="chend-line">— 全书完 —</div>';
  $('#content').appendChild(e);
}
/* 朗读快读完当前内容：先把下一章拼好，顺带预热它第一句 */
function warmNextAudio() {
  if (S.tts.engine === 'web' || !book) return;
  appendNext();
}
function unitOfSpan(i) {
  for (let k = 0; k < units.length; k++) if (i >= units[k].from && i <= units[k].to) return k;
  for (let k = 0; k < units.length; k++) if (units[k].from > i) return k;
  return Math.max(0, units.length - 1);
}
/* 定位一律瞬时，不要平滑动画（否则切章会看到“往上滚一下”） */
function setScroll(off) {
  const c = $('#content');
  const prev = c.style.scrollBehavior;
  c.style.scrollBehavior = 'auto';
  if (S.pageMode === 'slide') {
    const max = Math.max(0, c.scrollWidth - c.clientWidth);
    c.scrollLeft = off > 1 ? 0 : off * max;
  } else {
    c.scrollTop = off > 1 ? off : off * Math.max(0, c.scrollHeight - c.clientHeight);
  }
  void c.offsetHeight;
  c.style.scrollBehavior = prev;
}
/* 滚到当前章开头（切翻页方式/切章后对齐用） */
function scrollToChapterTop() {
  if (!chunks.length) return;
  const c = $('#content');
  const cur = chunks.find((x) => x.idx === chIdx) || chunks[0];
  const h = cur && cur.el.querySelector('.ch-head');
  if (!h) return;
  const cr = c.getBoundingClientRect(), hr = h.getBoundingClientRect();
  const prev = c.style.scrollBehavior;
  c.style.scrollBehavior = 'auto';
  if (S.pageMode === 'slide') {
    const W = c.clientWidth || 1;
    const target = c.scrollLeft + (hr.left - cr.left);
    c.scrollLeft = Math.round(target / W) * W;   // 左右模式对齐到整页
  } else {
    c.scrollTop += hr.top - cr.top - 6;
  }
  void c.offsetHeight;
  c.style.scrollBehavior = prev;
  updateProgress();
}
/* 视口顶部落在哪一章（拼接阅读时 chIdx 随滚动走） */
function syncChFromScroll() {
  if (!chunks.length) return;
  const c = $('#content'), cr = c.getBoundingClientRect();
  const cs = getComputedStyle(c);
  const padL = parseFloat(cs.paddingLeft) || 0, padT = parseFloat(cs.paddingTop) || 0;
  const slide = S.pageMode === 'slide';
  const W = c.clientWidth || 1;
  let cur = chunks[0];
  for (const ch of chunks) {
    const h = ch.el.querySelector('.ch-head');
    if (!h) continue;
    const r = h.getBoundingClientRect();
    // 左右模式按「第几页」比较，滚动/整页模式按「是否已到视口顶部」比较（都要扣掉内边距）
    const reached = slide ? Math.round((r.left - cr.left - padL) / W) <= 0 : r.top <= cr.top + padT + 6;
    if (reached) cur = ch;
    else break;
  }
  if (cur.idx !== chIdx) { chIdx = cur.idx; markTocCurrent(); }
  return cur;
}
/* 本章已读进度 0~1 */
function chapterFrac(cur) {
  if (!cur) return 0;
  const c = $('#content'), cr = c.getBoundingClientRect();
  const slide = S.pageMode === 'slide';
  const h = cur.el.querySelector('.ch-head');
  if (!h) return 0;
  const cs = getComputedStyle(c);
  const padL = parseFloat(cs.paddingLeft) || 0, padT = parseFloat(cs.paddingTop) || 0;
  const hr = h.getBoundingClientRect();
  const k = chunks.indexOf(cur);
  const nx = chunks[k + 1];
  const start = slide ? hr.left : hr.top;
  const pos = slide ? cr.left + padL : cr.top + padT;
  if (nx) {
    const nr = nx.el.querySelector('.ch-head').getBoundingClientRect();
    end = slide ? nr.left : nr.top;
  } else {
    end = slide ? cr.right : cr.bottom;
  }
  if (end <= start) return 0;
  return Math.min(1, Math.max(0, (pos - start) / (end - start)));
}
function updateProgress() {
  if (!chunks.length) return;
  const cur = syncChFromScroll();
  const frac = chapterFrac(cur);
  const total = chCount ? (chIdx + frac) / chCount : 0;
  const pct = Math.min(100, Math.round(total * 100));
  $('#progText').textContent = pct + '%';
  $('#progBar').style.width = pct + '%';
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    if (!book) return;
    const c = $('#content');
    const ccs = getComputedStyle(c);
    const off = S.pageMode === 'slide'
      ? frac
      : Math.max(0, c.getBoundingClientRect().top + (parseFloat(ccs.paddingTop) || 0) - (cur ? cur.el.querySelector('.ch-head').getBoundingClientRect().top : 0));
    api(`/api/books/${book.id}/progress`, {
      method: 'POST', body: JSON.stringify({ ch: chIdx, off })
    }).catch(() => { });
  }, 900);
}

/* ---------------- 目录 ---------------- */
function renderToc() {
  const list = $('#tocList');
  list.innerHTML = '';
  book.chapters.forEach((c) => {
    const d = document.createElement('div');
    d.className = 'toc-item';
    d.dataset.idx = c.idx;
    d.innerHTML = `<span class="n">${c.idx + 1}</span><span>${escapeHtml(c.title)}</span>`;
    d.onclick = () => { closeDrawer(); loadChapter(c.idx, 0); };
    list.appendChild(d);
  });
}
function markTocCurrent() {
  $$('#tocList .toc-item').forEach((el) =>
    el.classList.toggle('cur', +el.dataset.idx === chIdx));
}
function openDrawer() {
  $('#tocDrawer').classList.remove('hide');
  $('#mask').classList.remove('hide');
  const cur = $('#tocList .cur');
  if (cur) cur.scrollIntoView({ block: 'center' });
}
function closeDrawer() {
  $('#tocDrawer').classList.add('hide');
  $('#mask').classList.add('hide');
  $('#searchHits').classList.add('hide');
  $('#tocList').classList.remove('hide');
  $('#tocSearch').value = '';
}

/* ---------------- 朗读 ---------------- */
let speaking = false, curIdx = -1, audioEl = new Audio(), audioCache = new Map();
let currentBlobUrl = null;

function ttsCfg() {
  return { ...S.tts, engine: S.tts.engine, voice: S.tts.voice, speed: S.tts.speed };
}
function setHint(t) { $('#ttsHint').textContent = t || ''; }

async function startFrom(u) {
  if (!units.length) { setHint('本章没有可朗读的文字'); stopSpeak(); return; }
  if (u < 0) u = 0;
  if (u >= units.length) {
    // 读到当前内容末尾 → 把下一章拼到后面接着读（不重排、不跳转）
    const last = chunks[chunks.length - 1];
    if (S.tts.autoNext !== false && last && last.idx + 1 < chCount) {
      setHint('继续下一章…');
      const ok = await appendNext(false);
      if (ok && units.length > u) return startFrom(u);
      stopSpeak();
      setHint('下一章加载失败');
      return;
    }
    stopSpeak();
    setHint(chIdx + 1 >= chCount ? '全书读完' : '本章读完');
    return;
  }
  curIdx = u;
  const un = units[u];
  spans.forEach((s, k) => {
    s.classList.toggle('active', k >= un.from && k <= un.to);
    s.classList.toggle('done', k < un.from);
  });
  scrollToSpan(spans[un.from]);
  const text = un.text;
  $('#ttsNow').textContent = text.length > 60 ? text.slice(0, 60) + '…' : text;
  setHint(`${u + 1}/${units.length} · ${S.tts.engine === 'web' ? '浏览器语音' : 'HTTP 接口'}`);

  if (u >= units.length - 2) warmNextAudio();
  if (S.tts.engine === 'web') return speakWeb(text);
  return speakHttp(text);
}

/* 让正在朗读的句子进入视野（左右翻页模式下按列跳页） */
function scrollToSpan(sp) {
  if (!sp) return;
  const c = $('#content');
  if (S.pageMode === 'slide') {
    const cr = c.getBoundingClientRect(), r = sp.getBoundingClientRect();
    const x = r.left - cr.left + c.scrollLeft;
    const W = c.clientWidth || 1;
    const page = Math.max(0, Math.round(x / W));
    c.scrollTo({ left: page * W, behavior: 'smooth' });
  } else {
    sp.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }
}

function speakWeb(text) {
  const u = new SpeechSynthesisUtterance(text);
  u.lang = 'zh-CN';
  u.rate = Math.max(0.5, Math.min(2, +S.tts.speed || 1));
  const v = $('#webVoice').value;
  const voices = speechSynthesis.getVoices();
  const pick = voices.find((x) => x.voiceURI === v) || voices.find((x) => /zh/i.test(x.lang));
  if (pick) u.voice = pick;
  u.onend = () => { if (speaking) startFrom(curIdx + 1); };
  u.onerror = (e) => {
    if (e.error === 'interrupted' || e.error === 'canceled') return;
    setHint('浏览器语音出错：' + e.error + '（可在设置里换回 HTTP 接口）');
    speaking = false;
    $('#ttsPlay').textContent = '▶';
  };
  speechSynthesis.cancel();
  speechSynthesis.speak(u);
}

async function getAudio(text) {
  if (audioCache.has(text)) return audioCache.get(text);
  const r = await fetch('/api/tts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, voice: S.tts.voice, speed: S.tts.speed })
  });
  if (!r.ok) {
    let msg = 'HTTP ' + r.status;
    try { const j = await r.json(); msg = j.error + (j.body ? '\n' + j.body.slice(0, 160) : ''); } catch (e) { }
    throw new Error(msg);
  }
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  audioCache.set(text, url);
  return url;
}

let ttsErrs = 0;
async function speakHttp(text) {
  let url;
  try {
    setHint('合成中…');
    url = await getAudio(text);
  } catch (e) {
    ttsErrs++;
    // 单段合成失败不中断整篇：跳过继续读
    if (ttsErrs < 3 && speaking && curIdx + 1 < units.length) {
      setHint(`这段合成失败（${e.message}），已跳过`);
      setTimeout(() => { if (speaking) startFrom(curIdx + 1); }, 400);
      return;
    }
    setHint('失败：' + e.message);
    toast('朗读失败：' + e.message, 'err', 5000);
    speaking = false;
    $('#ttsPlay').textContent = '▶';
    return;
  }
  currentBlobUrl = url;
  audioEl.src = url;
  try {
    await audioEl.play();
  } catch (e) {
    // 自动播放被浏览器拦截（需要一次用户点击），不是合成失败
    setHint('浏览器拦截了自动播放，点一下 ▶ 继续');
    speaking = false;
    $('#ttsPlay').textContent = '▶';
    return;
  }
  ttsErrs = 0;
  audioEl.onended = () => { if (speaking) startFrom(curIdx + 1); };
  setHint(`${curIdx + 1}/${units.length} · 播放中`);
  // 预取后两段
  for (let k = 1; k <= 2; k++) {
    const n = units[curIdx + k];
    if (n && !audioCache.has(n.text)) getAudio(n.text).catch(() => { });
  }
}

function toggleSpeak() {
  if (speaking) {
    speaking = false;
    audioEl.pause();
    speechSynthesis.cancel();
    $('#ttsPlay').textContent = '▶';
    setHint('已暂停');
  } else {
    speaking = true;
    $('#ttsPlay').textContent = '⏸';
    startFrom(curIdx >= 0 ? curIdx : 0);
  }
}
function stopSpeak() {
  speaking = false;
  ttsErrs = 0;
  audioEl.pause();
  speechSynthesis.cancel();
  $('#ttsPlay').textContent = '▶';
  spans.forEach((s) => s.classList.remove('active', 'done'));
}

/* ---------------- 翻页方式：滚动 / 整页 / 左右 ---------------- */
function applyPageMode() {
  const c = $('#content');
  const m = S.pageMode || 'scroll';
  c.classList.toggle('mode-page', m === 'page');
  c.classList.toggle('mode-slide', m === 'slide');
  document.body.dataset.page = m;
  if (m === 'slide') {
    const pad = +S.padding || 0;
    const w = Math.max(180, c.clientWidth - pad * 2);
    c.style.columnWidth = w + 'px';
    c.style.columnGap = (pad * 2) + 'px';
  } else {
    c.style.columnWidth = '';
    c.style.columnGap = '';
  }
  $$('#pageModes button').forEach((b) => b.classList.toggle('on', b.dataset.pm === m));
}
async function turnPage(dir) {
  const c = $('#content');
  const slide = S.pageMode === 'slide';
  const W = c.clientWidth || 1;
  const step = slide ? W : Math.max(120, c.clientHeight - (S.padding || 0) * 2);
  const pos = () => (slide ? c.scrollLeft : c.scrollTop);
  const max = () => Math.max(0, (slide ? c.scrollWidth : c.scrollHeight) - (slide ? c.clientWidth : c.clientHeight));
  if (dir < 0 && pos() <= 8) { loadChapter(chIdx - 1, 0); return; }
  if (dir > 0 && pos() >= max() - 8) {
    // 到了已拼接内容的末尾：先把下一章接上，接着翻（不跳转、不清屏）
    if (await appendNext(false)) await new Promise((r) => requestAnimationFrame(r));
    if (pos() >= max() - 8) { toast('已经是最后一章', '', 1400); return; }
  }
  const target = Math.max(0, Math.min(max(), pos() + dir * step));
  if (slide) c.scrollTo({ left: Math.round(target / W) * W, behavior: 'smooth' });
  else c.scrollTo({ top: target, behavior: 'smooth' });
}

/* ---------------- 设置面板 ---------------- */
function openSet(tab = 'read') {
  $('#setPanel').classList.remove('hide');
  $('#mask').classList.remove('hide');
  switchTab(tab);
  fillSetForm();
}
function closeSet() {
  $('#setPanel').classList.add('hide');
  $('#mask').classList.add('hide');
}
function switchTab(t) {
  $$('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === t));
  $('#tab-read').classList.toggle('hide', t !== 'read');
  $('#tab-tts').classList.toggle('hide', t !== 'tts');
}
function fillSetForm() {
  $('#setFontSize').value = S.fontSize;
  $('#fsVal').textContent = S.fontSize + 'px';
  $('#setLineHeight').value = S.lineHeight;
  $('#lhVal').textContent = (+S.lineHeight).toFixed(2);
  $('#setPadding').value = S.padding;
  $('#pdVal').textContent = S.padding + 'px';
  $('#setFont').value = S.font;
  $$('#themes button').forEach((b) => b.classList.toggle('on', b.dataset.theme === S.theme));
  $$('#pageModes button').forEach((b) => b.classList.toggle('on', b.dataset.pm === (S.pageMode || 'scroll')));

  $('#ttsPreset').innerHTML = Object.entries(presets)
    .map(([k, p]) => `<option value="${k}">${escapeHtml(p.name)}</option>`).join('');
  $('#ttsEngine').value = S.tts.engine;
  $('#ttsUrl').value = S.tts.url || '';
  $('#ttsMethod').value = S.tts.method || 'POST';
  $('#ttsHeaders').value = S.tts.headers || '';
  $('#ttsBody').value = S.tts.body || '';
  $('#ttsVoice').value = S.tts.voice || '';
  $('#ttsProxy').value = S.tts.proxy || '';
  $('#ttsSpeed').value = S.tts.speed;
  $('#speedVal').textContent = (+S.tts.speed).toFixed(2);
  $('#ttsAutoNext').checked = S.tts.autoNext !== false;
  const ac = $('#autoCh');
  if (ac) ac.checked = S.autoCh !== false;
  $('#webVoiceRow').classList.toggle('hide', S.tts.engine !== 'web');
}
function applyPreset(key) {
  const p = presets[key];
  if (!p) return;
  S.tts.url = p.url; S.tts.method = p.method; S.tts.headers = p.headers;
  S.tts.body = p.body; S.tts.proxy = p.proxy || '';
  if (p.voice) S.tts.voice = p.voice;
  if (p.speed) S.tts.speed = p.speed;
  S.tts.engine = key === 'web' ? 'web' : 'http';
  fillSetForm();
  $('#presetNote').textContent = p.note || '';
  saveSettings();
}

/* ---------------- 事件绑定 ---------------- */

/* ================= 书源：搜索 / 详情 / 管理 ================= */
let srcIndex = null;      // 源名 -> id
let detailBook = null, detailSid = '', detailToc = [];

function showView(id) {
  for (const v of $$('.view')) v.classList.add('hide');
  const el = $('#' + id);
  if (el) el.classList.remove('hide');
}

async function loadSourceIndex(force) {
  if (srcIndex && !force) return srcIndex;
  const r = await api('/api/sources');
  srcIndex = {};
  for (const s of r.sources || []) srcIndex[s.name] = s.id;
  return srcIndex;
}

async function doSearch() {
  const kw = $('#searchInput').value.trim();
  if (!kw) return;
  const box = $('#searchRes');
  box.innerHTML = '<p class="hint" style="padding:14px">正在并发搜索，源多时约 20–60 秒…</p>';
  $('#searchStat').textContent = '';
  try {
    const r = await api('/api/src/search?q=' + encodeURIComponent(kw));
    const st = r.stats || {};
    $('#searchStat').textContent =
      `命中 ${st.ok}/${st.sources} 个源 · ${st.books} 本 · ${((st.ms || 0) / 1000).toFixed(1)}s`;
    box.innerHTML = '';
    const groups = (r.results || []).filter((g) => g.books.length);
    if (!groups.length) {
      box.innerHTML = '<p class="hint" style="padding:14px">没搜到。换个书名试试，或检查「书源」里哪些源还活着。</p>';
      return;
    }
    for (const g of groups) {
      const h = document.createElement('div');
      h.className = 'res-group';
      h.innerHTML = `<b>${escapeHtml(g.source)}</b> ${g.books.length} 本`;
      box.appendChild(h);
      for (const b of g.books.slice(0, 12)) {
        const it = document.createElement('div');
        it.className = 'res-item';
        const cover = b.cover
          ? `<img class="thumb" src="${escapeHtml(b.cover)}" referrerpolicy="no-referrer" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'thumb',textContent:'无封面'}))">`
          : '<div class="thumb">无封面</div>';
        it.innerHTML = cover + `<div class="info"><div class="t">${escapeHtml(b.name)}</div>
          <div class="m">${escapeHtml([b.author, b.kind, b.lastChapter].filter(Boolean).join(' · '))}</div></div>`;
        it.onclick = () => openDetail(g.source, b);
        box.appendChild(it);
      }
    }
  } catch (e) {
    box.innerHTML = `<p class="hint" style="padding:14px">搜索失败：${escapeHtml(e.message)}</p>`;
  }
}

async function openDetail(sourceName, b) {
  detailBook = b;
  showView('detail');
  $('#detailTitle').textContent = b.name;
  $('#detailBody').innerHTML = '<p class="hint">正在读取详情与目录，约 2–10 秒…</p>';
  await loadSourceIndex();
  detailSid = srcIndex[sourceName] || '';
  let info = {};
  let toc = { chapters: [] };
  try {
    const r = await api(`/api/src/info?sid=${detailSid}&url=${encodeURIComponent(b.url)}`);
    info = r.info || {};
    if (r.error) toc.error = r.error;
  } catch (e) { info = {}; }
  try {
    const t = await api(`/api/src/toc?sid=${detailSid}&url=${encodeURIComponent(b.url)}&toc=${encodeURIComponent(info.tocUrl || '')}`);
    toc = t;
  } catch (e) { toc = { error: e.message }; }
  renderDetail(b, info, toc);
}

function renderDetail(b, info, toc) {
  const body = $('#detailBody');
  const name = info.name || b.name;
  const author = (info.author || b.author || '').replace(/^作者[:：]\s*/, '');
  const kind = (info.kind || b.kind || '').replace(/^分类[:：]\s*/, '');
  const intro = (info.intro || b.intro || '').replace(/^简介[:：]\s*/, '').slice(0, 900);
  const cover = info.coverUrl || b.cover || '';
  const chs = toc.chapters || [];
  detailToc = chs;
  const coverHtml = cover
    ? `<img class="cover" src="${escapeHtml(cover)}" referrerpolicy="no-referrer" onerror="this.style.visibility='hidden'">`
    : '<div class="cover"></div>';
  body.innerHTML = `
    <div class="d-head">${coverHtml}
      <div class="meta">
        <h2>${escapeHtml(name)}</h2>
        <div class="line">${escapeHtml(author || '佚名')}</div>
        ${kind ? `<div class="line">${escapeHtml(kind)}</div>` : ''}
        ${info.lastChapter ? `<div class="line">源站最新章节：${escapeHtml(info.lastChapter)}</div>` : ''}
        <div class="line">${chs.length ? chs.length + ' 章' : (toc.error ? '目录失败' : '目录为空')}</div>
      </div>
    </div>
    ${intro ? `<div class="d-intro">${escapeHtml(intro)}</div>` : ''}
    <div class="d-actions">
      <button class="btn primary" id="btnAddShelf">加入书架</button>
      ${chs.length ? '<button class="btn" id="btnPreview">试读第一章</button>' : ''}
    </div>
    ${toc.error ? `<p class="hint">目录抓取失败：${escapeHtml(toc.error)}</p>` : ''}
    ${chs.length ? `<div class="ch-head">目录（前 200 章，共 ${chs.length} 章）</div>
      <div class="ch-list">${chs.slice(0, 200).map((c, i) => `<a data-i="${i}">${escapeHtml(c.name)}</a>`).join('')}</div>` : ''}
  `;
  body.querySelectorAll('.ch-list a').forEach((a) => {
    a.onclick = () => previewChapter(+a.dataset.i);
  });
  $('#btnAddShelf').onclick = addToShelf;
  const pv = $('#btnPreview');
  if (pv) pv.onclick = () => previewChapter(0);
}

async function previewChapter(i) {
  const c = detailToc[i];
  if (!c) return;
  toast('正在抓正文…', '', 30000);
  try {
    const r = await api(`/api/src/ch?sid=${detailSid}&url=${encodeURIComponent(c.url)}&book=${encodeURIComponent(detailBook.url)}`);
    $('#toast').classList.add('hide');
    if (r.error) { toast('抓取失败：' + r.error, 'err', 4000); return; }
    openTextPreview(c.name || detailBook.name, r.content || '');
  } catch (e) { toast('抓取失败：' + e.message, 'err', 4000); }
}

function openTextPreview(title, text) {
  book = { id: -1, title, chapters: [{ idx: 0, title }] };
  chIdx = 0; chCount = 1;
  previewing = true;
  $('#bookTitle').textContent = title;
  showView('reader');
  resetContent();
  appendChunk(0, { title, paras: String(text).split('\n') });
  applyPageMode();
  setScroll(0);
  updateProgress();
  $('#tocList').innerHTML = '';
}

async function addToShelf() {
  if (!detailBook) return;
  const btn = $('#btnAddShelf');
  btn.disabled = true;
  btn.textContent = '正在取目录…';
  try {
    const r = await api('/api/src/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sid: detailSid,
        book: detailBook,
        tocUrl: (detailToc && detailToc.length) ? '' : '',
        chapters: detailToc.length ? detailToc : undefined,
      }),
    });
    toast(`已加入书架：${r.chapters} 章`, 'ok');
    await loadShelf();
    showView('shelf');
    setTimeout(() => openBook(r.id), 350);
  } catch (e) {
    toast('加入失败：' + e.message, 'err', 4000);
    btn.disabled = false;
    btn.textContent = '加入书架';
  }
}

async function openSources() {
  showView('srcmgr');
  const box = $('#srcList');
  box.innerHTML = '<p class="hint">加载中…</p>';
  try {
    const r = await api('/api/sources');
    $('#srcStat').textContent = `共 ${r.total} 个源，启用 ${r.enabled} 个。关掉的源不参与搜索，搜索更快。`;
    srcIndex = {};
    for (const s of r.sources) srcIndex[s.name] = s.id;
    box.innerHTML = '';
    for (const s of r.sources) {
      const row = document.createElement('div');
      row.className = 'src-row';
      row.innerHTML = `
        <button class="sw ${s.enabled ? 'on' : ''}" title="启用/停用"></button>
        <div class="n"><b>${escapeHtml(s.name)}</b><span>${escapeHtml((s.url || '').replace(/^https?:\/\//, '').slice(0, 40))}</span></div>
        <button class="mini">测试</button>`;
      row.querySelector('.sw').onclick = async (ev) => {
        const b = ev.currentTarget;
        const res = await api(`/api/sources/${s.id}/toggle`, { method: 'POST' });
        b.classList.toggle('on', res.enabled);
        s.enabled = res.enabled;
      };
      row.querySelector('.mini').onclick = async (ev) => {
        const b = ev.currentTarget;
        b.disabled = true; b.textContent = '测试中';
        try {
          const t = await api(`/api/sources/${s.id}/test`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: $('#searchInput').value.trim() || '剑来' }),
          });
          const n = t.count != null ? t.count : (t.books || []).length;
          b.textContent = n ? n + ' 本' : '无结果';
          toast(n ? `${s.name}：${n} 本` : `${s.name}：${t.error || '没搜到'}`, n ? 'ok' : 'err', 3500);
        } catch (e) {
          b.textContent = '失败';
          toast(s.name + '：' + e.message, 'err', 3500);
        }
        setTimeout(() => { b.disabled = false; b.textContent = '测试'; }, 2500);
      };
      box.appendChild(row);
    }
  } catch (e) {
    box.innerHTML = `<p class="hint">加载失败：${escapeHtml(e.message)}</p>`;
  }
}

function exportSources(only) {
  const url = `/api/sources/export?only=${only ? 1 : 0}`;
  const a = document.createElement('a');
  a.href = url;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast(only ? '正在导出启用中的书源…' : '正在导出全部书源…', 'ok', 2500);
}

async function importSourceFile(f) {
  toast('正在导入书源…', '', 30000);
  try {
    const r = await api('/api/sources/import', { method: 'POST', body: f });
    toast(`导入完成：新增 ${r.added}，更新 ${r.updated}，共 ${r.total} 个`, 'ok', 4000);
    srcIndex = null;
    openSources();
  } catch (e) {
    toast('导入失败：' + e.message, 'err', 4000);
  }
}

function bind() {
  $('#btnImport').onclick = () => $('#fileInput').click();
  $('#btnSrcExportOn').onclick = () => exportSources(true);
  $('#btnSrcExportAll').onclick = () => exportSources(false);
  $('#btnDiscover').onclick = () => { showView('discover'); $('#searchInput').focus(); };
  $('#btnDiscBack').onclick = () => showView('shelf');
  $('#btnDoSearch').onclick = doSearch;
  $('#searchInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });
  $('#btnDetailBack').onclick = () => showView('discover');
  $('#btnSources').onclick = openSources;
  $('#btnSrcBack').onclick = () => showView('shelf');
  $('#btnSrcImport').onclick = () => $('#srcFile').click();
  $('#srcFile').onchange = (e) => { const f = e.target.files[0]; if (f) importSourceFile(f); e.target.value = ''; };
  $('#fileInput').onchange = (e) => { importFiles([...e.target.files]); e.target.value = ''; };
  $('#btnShelfSet').onclick = () => openSet('read');
  $('#btnBack').onclick = () => { stopSpeak(); showView('shelf'); loadShelf(); };
  $('#btnToc').onclick = openDrawer;
  $('#btnTocClose').onclick = closeDrawer;
  $('#mask').onclick = () => { closeDrawer(); closeSet(); };
  $('#btnReaderSet').onclick = () => openSet('read');
  $('#btnPrev').onclick = () => loadChapter(chIdx - 1, 0);
  $('#btnNext').onclick = () => loadChapter(chIdx + 1, 0);
  let scrollRaf = 0;
  $('#content').addEventListener('scroll', () => {
    if (scrollRaf) return;
    scrollRaf = requestAnimationFrame(() => {
      scrollRaf = 0;
      updateProgress();
      ensureAhead();
    });
  }, { passive: true });

  // 点击正文：点句子→从这句读；翻页模式下左右两侧→翻页；中间→开关朗读面板
  let touchY = 0, touchX = 0, touchT = 0, swiped = false;
  $('#content').addEventListener('touchstart', (e) => {
    touchX = e.touches[0].clientX; touchY = e.touches[0].clientY;
    touchT = Date.now(); swiped = false;
  }, { passive: true });
  $('#content').addEventListener('touchend', (e) => {
    const t = e.changedTouches[0];
    const dx = t.clientX - touchX, dy = t.clientY - touchY;
    if (Date.now() - touchT > 800) return;
    if (Math.abs(dx) < 45 && Math.abs(dy) < 45) return;
    swiped = true;
    if (S.pageMode === 'scroll') return;
    if (S.pageMode === 'slide') {
      if (Math.abs(dx) > Math.abs(dy)) turnPage(dx < 0 ? 1 : -1);
      return;
    }
    if (Math.abs(dy) > Math.abs(dx)) turnPage(dy < 0 ? 1 : -1);
  }, { passive: true });

  $('#content').addEventListener('click', (e) => {
    if (swiped) { swiped = false; return; }
    if (e.target.classList.contains('s')) {
      // 点某句 → 从这句所在单元开始朗读
      const i = +e.target.dataset.i;
      speaking = true; $('#ttsPlay').textContent = '⏸';
      setTtsPanel(true);
      startFrom(unitOfSpan(i));
      return;
    }
    const sel = window.getSelection();
    if (sel && sel.toString().length > 0) return;
    if (S.pageMode !== 'scroll') {
      const r = $('#content').getBoundingClientRect();
      const x = (e.clientX - r.left) / (r.width || 1);
      if (x > 0.62) { turnPage(1); return; }
      if (x < 0.38) { turnPage(-1); return; }
    }
    setTtsPanel($('#ttsPanel').classList.contains('hide'));
  });

  $('#btnSpeak').onclick = () => {
    setTtsPanel(true);
    toggleSpeak();
  };
  $('#ttsPlay').onclick = toggleSpeak;
  $('#ttsPrev').onclick = () => { speaking = true; $('#ttsPlay').textContent = '⏸'; startFrom(Math.max(0, curIdx - 1)); };
  $('#ttsNext').onclick = () => { speaking = true; $('#ttsPlay').textContent = '⏸'; startFrom(curIdx + 1); };
  $('#ttsSpeed').oninput = (e) => {
    S.tts.speed = +e.target.value;
    $('#speedVal').textContent = S.tts.speed.toFixed(2);
    saveSettings();
  };
  $('#ttsEngine').onchange = (e) => {
    S.tts.engine = e.target.value;
    $('#webVoiceRow').classList.toggle('hide', S.tts.engine !== 'web');
    saveSettings();
  };
  $('#btnTtsCfg').onclick = () => { closeDrawer(); openSet('tts'); };

  // 设置面板
  $$('.tab').forEach((b) => b.onclick = () => switchTab(b.dataset.tab));
  $('#setFontSize').oninput = (e) => { S.fontSize = +e.target.value; $('#fsVal').textContent = S.fontSize + 'px'; applyStyle(); };
  $('#setLineHeight').oninput = (e) => { S.lineHeight = +e.target.value; $('#lhVal').textContent = S.lineHeight.toFixed(2); applyStyle(); };
  $('#setPadding').oninput = (e) => { S.padding = +e.target.value; $('#pdVal').textContent = S.padding + 'px'; applyStyle(); };
  $('#setFont').onchange = (e) => { S.font = e.target.value; applyStyle(); };
  $$('#pageModes button').forEach((b) => b.onclick = () => {
    S.pageMode = b.dataset.pm;
    applyPageMode();
    requestAnimationFrame(() => { scrollToChapterTop(); ensureAhead(); });
    saveSettings();
    toast('翻页方式：' + b.textContent);
  });
  $('#ttsAutoNext').onchange = (e) => { S.tts.autoNext = e.target.checked; saveSettings(); };
  $('#autoCh').onchange = (e) => {
    S.autoCh = e.target.checked;
    saveSettings();
    if (S.autoCh) ensureAhead();
  };
  window.addEventListener('resize', () => {
    if (S.pageMode !== 'slide') return;
    const c = $('#content');
    const max = Math.max(0, c.scrollWidth - c.clientWidth);
    const ratio = max > 0 ? c.scrollLeft / max : 0;
    applyPageMode();
    c.scrollLeft = ratio * Math.max(0, c.scrollWidth - c.clientWidth);
  });
  $$('#themes button').forEach((b) => b.onclick = () => {
    S.theme = b.dataset.theme; applyStyle();
    $$('#themes button').forEach((x) => x.classList.toggle('on', x === b));
  });
  $('#ttsPreset').onchange = (e) => applyPreset(e.target.value);
  $('#btnSaveSet').onclick = async () => {
    S.tts.url = $('#ttsUrl').value.trim();
    S.tts.method = $('#ttsMethod').value;
    S.tts.headers = $('#ttsHeaders').value;
    S.tts.body = $('#ttsBody').value;
    S.tts.voice = $('#ttsVoice').value.trim();
    S.tts.proxy = $('#ttsProxy').value.trim();
    await saveSettings();
    audioCache.clear();
    toast('已保存', 'ok');
    closeSet();
  };
  $('#btnTestTts').onclick = async () => {
    const cfg = {
      url: $('#ttsUrl').value.trim(), method: $('#ttsMethod').value,
      headers: $('#ttsHeaders').value, body: $('#ttsBody').value,
      proxy: $('#ttsProxy').value.trim(), engine: 'http'
    };
    const voice = $('#ttsVoice').value.trim();
    $('#testResult').textContent = '测试中…';
    try {
      const r = await api('/api/tts/test', {
        method: 'POST',
        body: JSON.stringify({ text: '你好，这是一次朗读测试。', voice, speed: +S.tts.speed, cfg })
      });
      if (r.ok) {
        $('#testResult').innerHTML =
          `<span style="color:var(--ok)">✓ 成功</span> ${fmtBytes(r.bytes)} · ${r.content_type} · ${r.ms}ms`;
        // 顺便保存这份配置，然后试听
        S.tts = { ...S.tts, ...cfg, voice };
        await saveSettings();
        const resp = await fetch('/api/tts', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: '你好，这是一次朗读测试。', voice, speed: +S.tts.speed, cache: false })
        });
        if (resp.ok) { audioEl.src = URL.createObjectURL(await resp.blob()); audioEl.play(); }
      } else {
        $('#testResult').innerHTML =
          `<span style="color:var(--err)">✗ ${escapeHtml(r.error || '失败')}</span>` +
          (r.status ? ` HTTP ${r.status}` : '') +
          (r.body ? `<br><code>${escapeHtml(String(r.body).slice(0, 200))}</code>` : '');
      }
    } catch (e) {
      $('#testResult').innerHTML = `<span style="color:var(--err)">✗ ${escapeHtml(e.message)}</span>`;
    }
  };

  // 目录搜索
  let st = null;
  $('#tocSearch').oninput = (e) => {
    clearTimeout(st);
    const kw = e.target.value.trim();
    if (!kw) { $('#searchHits').classList.add('hide'); $('#tocList').classList.remove('hide'); return; }
    st = setTimeout(async () => {
      const { hits } = await api(`/api/books/${book.id}/search?q=${encodeURIComponent(kw)}`);
      const box = $('#searchHits');
      box.innerHTML = hits.length ? '' : '<p class="empty">没搜到</p>';
      hits.forEach((h) => {
        const d = document.createElement('div');
        d.className = 'toc-item';
        d.innerHTML = `<span class="n">${h.idx + 1}</span><span><b>${escapeHtml(h.title)}</b><br>
          <span class="sn">${escapeHtml(h.snippet)}</span></span>`;
        d.onclick = () => { closeDrawer(); loadChapter(h.idx, 0); };
        box.appendChild(d);
      });
      box.classList.remove('hide');
      $('#tocList').classList.add('hide');
    }, 350);
  };

  // 系统语音列表
  const fillVoices = () => {
    const sel = $('#webVoice');
    if (!sel) return;
    sel.innerHTML = speechSynthesis.getVoices()
      .map((v) => `<option value="${v.voiceURI}">${escapeHtml(v.name)} (${v.lang})</option>`).join('');
  };
  if ('speechSynthesis' in window) {
    speechSynthesis.onvoiceschanged = fillVoices;
    fillVoices();
  }
  document.addEventListener('keydown', (e) => {
    if ($('#reader').classList.contains('hide')) return;
    if (e.key === 'PageDown') { e.preventDefault(); loadChapter(chIdx + 1, 0); return; }
    if (e.key === 'PageUp') { e.preventDefault(); loadChapter(chIdx - 1, 0); return; }
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      const d = e.key === 'ArrowRight' ? 1 : -1;
      if (S.pageMode === 'scroll') loadChapter(chIdx + d, 0);
      else turnPage(d);
      return;
    }
    if (e.key === ' ') { e.preventDefault(); toggleSpeak(); }
  });
}

/* ---------------- 启动 ---------------- */
(async function init() {
  bind();
  await loadSettings();
  applyPageMode();
  try { presets = (await api('/api/presets')).presets; } catch (e) { }
  await loadShelf();
})();
