/* ==========================================================================
   TTSNover · 现代极简美学阅读器前端逻辑
   ========================================================================== */

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const DEFAULTS = {
  fontSize: 19,
  lineHeight: 1.85,
  padding: 20,
  font: 'system',
  theme: 'dark',
  pageMode: 'scroll',       // scroll 垂直滚动 | page 仿真整页 | slide 横向多栏
  autoCh: true,             // 滚动到底无缝自动拼接下一章
  tts: {
    engine: 'http',
    url: '',
    method: 'POST',
    headers: '',
    body: '',
    voice: '',
    speed: 1.18,
    proxy: 'http://127.0.0.1:7890',
    autoNext: true,
    provider: '',            // mimo 等走专用协议的服务商
    model: '',
    style: '',
    clientSpeed: false,      // true = 语速由前端 playbackRate 控制，改速不重新合成
    mimoMode: 'preset',      // 小米音色来源：preset 预置 | design 音色设计 | clone 音色克隆
    mimoVoiceDesc: '',       // 音色设计：文字描述
    mimoClone: '',           // 音色克隆：样本名（存服务端 data/mimo_clones）
    profiles: {}             // 每个服务商各存一份配置
  }
};

let S = JSON.parse(JSON.stringify(DEFAULTS));
let book = null, chIdx = 0, chCount = 0, sentences = [], spans = [], units = [];
let previewing = false;
let chunks = [];            // 已拼接进正文的章节块（连续阅读）
let prefetched = new Map(); // idx -> 已提前取回的章节数据
let appending = false, endMarked = false;
let presets = {}, mimoVoices = [], mimoReady = false, mimoKeyMasked = '';
let mimoModes = [], mimoClones = [], mimoDesignDefault = '', mimoCloneMax = 7 * 1024 * 1024;
let saveTimer = null;
let shelfBooksList = [];    // 书架本地缓存列表，用于即时搜索过滤
let sourcesList = [];       // 书源列表缓存
let srcMulti = false;       // 多选模式（批量删除）
const srcSel = new Set();   // 多选模式下已选中的书源 id
let curSrcList = [];        // 当前渲染出来的列表（受筛选影响）

/* ---------------- 基础工具函数 ---------------- */
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (r.status === 401) {
    // 会话过期/未登录：整页跳登录，别让界面卡在空数据上
    const back = encodeURIComponent(location.pathname + location.search);
    location.replace('/login?next=' + back);
    throw new Error('请先登录');
  }
  const ct = r.headers.get('content-type') || '';
  if (ct.includes('application/json')) {
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    return j;
  }
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r;
}

function toast(msg, kind = '', ms = 2800) {
  const t = $('#toast');
  if (!t) return;
  t.textContent = msg;
  t.className = 'toast ' + (kind || '');
  clearTimeout(t._t);
  t._t = setTimeout(() => t.classList.add('hide'), ms);
}

function fmtBytes(n) {
  if (n == null) return '0 B';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

/* 优雅自定义确认对话框 */
function showConfirm({ title = '确认操作', msg = '此操作无法撤销，是否继续？', okText = '确认', onOk }) {
  const modal = $('#confirmModal');
  const t = $('#confirmTitle'), m = $('#confirmMsg'), okBtn = $('#btnConfirmOk'), cancelBtn = $('#btnConfirmCancel');
  if (t) t.textContent = title;
  if (m) m.textContent = msg;
  if (okBtn) okBtn.textContent = okText;
  
  const close = () => {
    modal.classList.add('hide');
    okBtn.onclick = null;
    cancelBtn.onclick = null;
  };
  
  okBtn.onclick = () => {
    close();
    if (typeof onOk === 'function') onOk();
  };
  cancelBtn.onclick = close;
  modal.onclick = (e) => { if (e.target === modal) close(); };
  modal.classList.remove('hide');
}

/* ---------------- 设置加载与应用 ---------------- */
async function loadSettings() {
  try {
    const s = await api('/api/settings');
    if (s && Object.keys(s).length) {
      S = { ...DEFAULTS, ...s, tts: { ...DEFAULTS.tts, ...(s.tts || {}) } };
    }
  } catch (e) {
    console.warn('读取设置失败', e);
  }
  applyStyle();
}

async function saveSettings() {
  try {
    await api('/api/settings', { method: 'PUT', body: JSON.stringify(S) });
  } catch (e) {
    toast('保存设置失败：' + e.message, 'err');
  }
}

function applyStyle() {
  const r = document.documentElement.style;
  r.setProperty('--fs', S.fontSize + 'px');
  r.setProperty('--lh', S.lineHeight);
  r.setProperty('--pd', S.padding + 'px');
  document.body.dataset.theme = S.theme || 'dark';
  document.body.dataset.font = S.font || 'system';

  // 更新设置弹窗内数值显示
  const fsVal = $('#fsVal'); if (fsVal) fsVal.textContent = S.fontSize + 'px';
  const setFs = $('#setFontSize'); if (setFs) setFs.value = S.fontSize;
  const lhVal = $('#lhVal'); if (lhVal) lhVal.textContent = (+S.lineHeight).toFixed(2);
  const setLh = $('#setLineHeight'); if (setLh) setLh.value = S.lineHeight;
  const pdVal = $('#pdVal'); if (pdVal) pdVal.textContent = S.padding + 'px';
  const setPd = $('#setPadding'); if (setPd) setPd.value = S.padding;
  const setFont = $('#setFont'); if (setFont) setFont.value = S.font;

  // 主题高亮激活
  $$('#themes .theme-card').forEach((b) => b.classList.toggle('on', b.dataset.theme === S.theme));

  if (S.pageMode === 'slide') applyPageMode();
}

/* ---------------- 书架模块 ---------------- */
async function loadShelf() {
  try {
    const { books } = await api('/api/books');
    shelfBooksList = books || [];
    renderShelfGrid(shelfBooksList);
  } catch (e) {
    toast('加载书架失败：' + e.message, 'err');
  }
}

function renderShelfGrid(books) {
  const grid = $('#grid');
  const empty = $('#shelfEmpty');
  grid.innerHTML = '';
  
  if (!books || books.length === 0) {
    empty.classList.remove('hide');
    return;
  }
  empty.classList.add('hide');

  for (const b of books) {
    const pct = b.ch_count ? Math.min(100, Math.round((b.progress_ch + (b.progress_off || 0)) / b.ch_count * 100)) : 0;
    const hue = (b.seed * 37) % 360;
    const card = document.createElement('div');
    card.className = 'card';
    
    // 生成质感渐变
    const grad = `linear-gradient(150deg, hsl(${hue} 48% 36%), hsl(${(hue + 45) % 360} 54% 18%))`;
    
    card.innerHTML = `
      <div class="cover" style="background:${grad}">
        <div class="cover-title">${escapeHtml(b.title)}</div>
        <div class="cover-bottom-tag">${pct > 0 ? `已读 ${pct}%` : '未开始'}</div>
      </div>
      <div class="name" title="${escapeHtml(b.title)}">${escapeHtml(b.title)}</div>
      <div class="meta">
        <span>${b.ch_count} 章</span>
        <span class="pct-tag">${pct}%</span>
      </div>
      <div class="card-prog-line">
        <div class="card-prog-fill" style="width:${pct}%"></div>
      </div>
      <button class="del" title="从书架移除">
        <svg class="svg-icon" style="width:14px;height:14px"><use href="#icon-trash"></use></svg>
      </button>`;

    // 删除事件
    card.querySelector('.del').onclick = (ev) => {
      ev.stopPropagation();
      showConfirm({
        title: '移除书籍',
        msg: `确定要从书架移除《${b.title}》吗？书籍文件和阅读进度将被删除。`,
        okText: '确认移除',
        onOk: async () => {
          try {
            await api('/api/books/' + b.id, { method: 'DELETE' });
            toast(`已移除《${b.title}》`, 'ok');
            loadShelf();
          } catch (err) {
            toast('删除失败：' + err.message, 'err');
          }
        }
      });
    };

    card.onclick = () => openBook(b.id);
    grid.appendChild(card);
  }
}

/* 过滤书架 */
function filterShelf(kw) {
  kw = (kw || '').trim().toLowerCase();
  if (!kw) {
    renderShelfGrid(shelfBooksList);
    return;
  }
  const filtered = shelfBooksList.filter((b) => (b.title || '').toLowerCase().includes(kw));
  renderShelfGrid(filtered);
}

/* ---------------- 本地文件导入 ---------------- */
async function importFiles(files) {
  if (!files || !files.length) return;
  for (const f of files) {
    toast(`正在导入《${f.name}》…`, '', 60000);
    try {
      const r = await api('/api/import?filename=' + encodeURIComponent(f.name), {
        method: 'POST', body: f
      });
      toast(`《${r.title}》导入成功，共 ${r.chapters} 章`, 'ok', 3500);
    } catch (e) {
      toast(`《${f.name}》导入失败：${e.message}`, 'err', 5000);
    }
  }
  loadShelf();
}

/* ---------------- 阅读器核心 ---------------- */
async function openBook(id) {
  try {
    book = await api('/api/books/' + id);
    chIdx = Math.min(book.progress_ch || 0, Math.max(0, book.chapters.length - 1));
    chCount = book.chapters.length;
    $('#bookTitle').textContent = book.title;
    const sub = $('#chapterSubTitle');
    if (sub) sub.textContent = '';
    $('#shelf').classList.add('hide');
    $('#reader').classList.remove('hide');
    renderToc();
    await loadChapter(chIdx, book.progress_off || 0);
  } catch (e) {
    toast('打开书籍失败：' + e.message, 'err');
  }
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

  // 更新顶部章节副标题
  const sub = $('#chapterSubTitle');
  if (sub) sub.textContent = d.title || `第 ${i + 1} 章`;

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
    updatePlayButtonUI(true);
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

/* 朗读语义单元断句 */
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
        buf += s;
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

function setTtsPanel(open) {
  $('#ttsPanel').classList.toggle('hide', !open);
  document.body.classList.toggle('tts-open', !!open);
  if (open) requestAnimationFrame(() => ensureAhead());
}

function readerOpen() {
  return !$('#reader').classList.contains('hide');
}

async function fetchCh(i) {
  if (prefetched.has(i)) {
    const d = prefetched.get(i);
    prefetched.delete(i);
    return d;
  }
  return api(`/api/books/${book.id}/ch/${i}`);
}

function prefetchAhead() {
  if (!book || book.id < 0 || previewing) return;
  const last = chunks[chunks.length - 1];
  if (!last) return;
  const n = last.idx + 1;
  if (n >= chCount || prefetched.has(n)) return;
  api(`/api/books/${book.id}/ch/${n}`).then((d) => prefetched.set(n, d)).catch(() => {});
}

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
    if (speaking && S.tts.engine !== 'web' && units[ch.baseUnit]) getAudio(units[ch.baseUnit].text).catch(() => {});
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
  e.innerHTML = '<div class="chend-line">— 全书完 · 感谢阅读 —</div>';
  $('#content').appendChild(e);
}

function warmNextAudio() {
  if (S.tts.engine === 'web' || !book) return;
  appendNext();
}

function unitOfSpan(i) {
  for (let k = 0; k < units.length; k++) if (i >= units[k].from && i <= units[k].to) return k;
  for (let k = 0; k < units.length; k++) if (units[k].from > i) return k;
  return Math.max(0, units.length - 1);
}

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
    c.scrollLeft = Math.round(target / W) * W;
  } else {
    c.scrollTop += hr.top - cr.top - 6;
  }
  void c.offsetHeight;
  c.style.scrollBehavior = prev;
  updateProgress();
}

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
    const reached = slide ? Math.round((r.left - cr.left - padL) / W) <= 0 : r.top <= cr.top + padT + 6;
    if (reached) cur = ch;
    else break;
  }
  if (cur.idx !== chIdx) {
    chIdx = cur.idx;
    markTocCurrent();
    const sub = $('#chapterSubTitle');
    if (sub) sub.textContent = cur.title || `第 ${chIdx + 1} 章`;
  }
  return cur;
}

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
  let end;
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
  
  const progText = $('#progText'); if (progText) progText.textContent = pct + '%';
  const progBar = $('#progBar'); if (progBar) progBar.style.width = pct + '%';
  const progChInfo = $('#progChInfo');
  if (progChInfo) progChInfo.textContent = chCount ? `第 ${chIdx + 1} / ${chCount} 章` : '';

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
    }).catch(() => {});
  }, 900);
}

/* ---------------- 目录抽屉 ---------------- */
function renderToc() {
  const list = $('#tocList');
  if (!list || !book) return;
  list.innerHTML = '';
  const chapters = book.chapters || [];
  const badge = $('#tocCountBadge');
  if (badge) badge.textContent = `共 ${chapters.length} 章`;

  chapters.forEach((c) => {
    const d = document.createElement('div');
    d.className = 'toc-item';
    d.dataset.idx = c.idx;
    d.innerHTML = `<span class="n">${c.idx + 1}</span><span>${escapeHtml(c.title)}</span>`;
    d.onclick = () => { closeDrawer(); loadChapter(c.idx, 0); };
    list.appendChild(d);
  });
}

function markTocCurrent() {
  $$('#tocList .toc-item').forEach((el) => el.classList.toggle('cur', +el.dataset.idx === chIdx));
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

/* ---------------- 朗读模块 (TTS & 灵动悬浮胶囊) ---------------- */
let speaking = false, curIdx = -1, audioEl = new Audio(), audioCache = new Map();

function setHint(t) {
  const h = $('#ttsHint'); if (h) h.textContent = t || '';
  const cm = $('#capsuleMeta'); if (cm) cm.textContent = t || '听书中…';
}

function updatePlayButtonUI(isPlaying) {
  const icon = isPlaying
    ? '<svg class="svg-icon play-big"><use href="#icon-pause"></use></svg>'
    : '<svg class="svg-icon play-big"><use href="#icon-play"></use></svg>';
  const capIcon = isPlaying ? '#icon-pause' : '#icon-play';
  
  const ttsPlay = $('#ttsPlay'); if (ttsPlay) ttsPlay.innerHTML = icon;
  const capSvg = $('#capsulePlayIcon');
  if (capSvg) capSvg.innerHTML = `<use href="${capIcon}"></use>`;
  const btnSpeak = $('#btnSpeak');
  if (btnSpeak) btnSpeak.innerHTML = `<svg class="svg-icon play-icon"><use href="${capIcon}"></use></svg>`;
  
  const capsule = $('#ttsCapsule');
  if (capsule) capsule.classList.toggle('playing', isPlaying);
}

function showCapsule() {
  const cap = $('#ttsCapsule');
  if (cap) {
    cap.classList.remove('hide');
    const speedText = $('#capsuleSpeedText');
    if (speedText) speedText.textContent = (+S.tts.speed).toFixed(2) + 'x';
    const capTitle = $('#capsuleTitle');
    if (capTitle) capTitle.textContent = book ? book.title : 'TTSNover';
  }
}

function hideCapsule() {
  const cap = $('#ttsCapsule');
  if (cap) cap.classList.add('hide');
}

async function startFrom(u) {
  if (!units.length) { setHint('本章没有可朗读的文字'); stopSpeak(); return; }
  if (u < 0) u = 0;
  if (u >= units.length) {
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
  const ttsNow = $('#ttsNow');
  if (ttsNow) ttsNow.textContent = text.length > 60 ? text.slice(0, 60) + '…' : text;
  
  showCapsule();
  const capTitle = $('#capsuleTitle');
  if (capTitle) capTitle.textContent = text.length > 36 ? text.slice(0, 36) + '…' : text;

  setHint(`${u + 1}/${units.length} · ${S.tts.engine === 'web' ? '浏览器语音' : '云端接口'}`);

  if (u >= units.length - 2) warmNextAudio();
  if (S.tts.engine === 'web') return speakWeb(text);
  return speakHttp(text);
}

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
  u.rate = Math.max(0.5, Math.min(2.5, +S.tts.speed || 1));
  const v = $('#webVoice') ? $('#webVoice').value : '';
  const voices = speechSynthesis.getVoices();
  const pick = voices.find((x) => x.voiceURI === v) || voices.find((x) => /zh/i.test(x.lang));
  if (pick) u.voice = pick;
  u.onend = () => { if (speaking) startFrom(curIdx + 1); };
  u.onerror = (e) => {
    if (e.error === 'interrupted' || e.error === 'canceled') return;
    setHint('系统语音出错：' + e.error);
    speaking = false;
    updatePlayButtonUI(false);
  };
  speechSynthesis.cancel();
  speechSynthesis.speak(u);
}

async function getAudio(text) {
  if (audioCache.has(text)) return audioCache.get(text);
  const r = await fetch('/api/tts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      text, voice: S.tts.voice, speed: S.tts.speed,
      provider: S.tts.provider || '', model: S.tts.model || '', style: S.tts.style || '',
      mimoMode: S.tts.mimoMode || 'preset',
      mimoVoiceDesc: S.tts.mimoVoiceDesc || '',
      mimoClone: S.tts.mimoClone || ''
    })
  });
  if (!r.ok) {
    let msg = 'HTTP ' + r.status;
    try { const j = await r.json(); msg = j.error + (j.body ? '\n' + j.body.slice(0, 160) : ''); } catch (e) {}
    throw new Error(msg);
  }
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  audioCache.set(text, url);
  return url;
}

/* 语速：小米这类服务端不吃 speed 的，用前端 playbackRate 变速（不重新合成） */
function applyPlaybackRate() {
  const sp = Math.max(0.5, Math.min(2, +S.tts.speed || 1));
  audioEl.preservesPitch = true;
  audioEl.playbackRate = S.tts.clientSpeed ? sp : 1;
}

let ttsErrs = 0;
async function speakHttp(text) {
  let url;
  try {
    setHint('合成音频中…');
    url = await getAudio(text);
  } catch (e) {
    ttsErrs++;
    if (ttsErrs < 3 && speaking && curIdx + 1 < units.length) {
      setHint(`合成失败跳过：${e.message}`);
      setTimeout(() => { if (speaking) startFrom(curIdx + 1); }, 400);
      return;
    }
    setHint('失败：' + e.message);
    toast('朗读失败：' + e.message, 'err', 5000);
    speaking = false;
    updatePlayButtonUI(false);
    return;
  }
  audioEl.src = url;
  applyPlaybackRate();
  try {
    await audioEl.play();
  } catch (e) {
    setHint('自动播放被阻止，点一下播放按钮继续');
    speaking = false;
    updatePlayButtonUI(false);
    return;
  }
  ttsErrs = 0;
  audioEl.onended = () => { if (speaking) startFrom(curIdx + 1); };
  setHint(`${curIdx + 1}/${units.length} · 正在播放`);
  
  // 预取下一段音频
  for (let k = 1; k <= 2; k++) {
    const n = units[curIdx + k];
    if (n && !audioCache.has(n.text)) getAudio(n.text).catch(() => {});
  }
}

function toggleSpeak() {
  if (speaking) {
    speaking = false;
    audioEl.pause();
    speechSynthesis.cancel();
    updatePlayButtonUI(false);
    setHint('已暂停播放');
  } else {
    speaking = true;
    updatePlayButtonUI(true);
    showCapsule();
    startFrom(curIdx >= 0 ? curIdx : 0);
  }
}

function stopSpeak() {
  speaking = false;
  ttsErrs = 0;
  audioEl.pause();
  speechSynthesis.cancel();
  updatePlayButtonUI(false);
  spans.forEach((s) => s.classList.remove('active', 'done'));
}

/* ---------------- 翻页模式 ---------------- */
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
    if (await appendNext(false)) await new Promise((r) => requestAnimationFrame(r));
    if (pos() >= max() - 8) { toast('已经是最后一章了', '', 1600); return; }
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
  $$('#themes .theme-card').forEach((b) => b.classList.toggle('on', b.dataset.theme === S.theme));
  $$('#pageModes button').forEach((b) => b.classList.toggle('on', b.dataset.pm === (S.pageMode || 'scroll')));

  $('#ttsPreset').innerHTML = Object.entries(presets)
    .map(([k, p]) => `<option value="${k}">${escapeHtml(p.name)}</option>`).join('');
  $('#ttsPreset').value = currentPresetKey();
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
  fillMimoForm();
}

/* 当前生效的是哪个预设（给下拉选值、以及按服务商各存一份配置） */
function currentPresetKey() {
  if (S.tts.engine === 'web') return 'web';
  if (S.tts.provider === 'mimo') return 'mimo';
  const u = S.tts.url || '';
  if (/fish\.audio/.test(u)) return 'fish';
  if (/siliconflow/.test(u)) return 'siliconflow';
  if (/speech\.microsoft/.test(u)) return 'azure';
  if (/audio\/speech/.test(u)) return 'openai';
  return '';
}

/* 小米面板：音色来源三选一（预置/设计/克隆）+ 风格 + Key 状态 */
function fillMimoForm() {
  const on = S.tts.provider === 'mimo';
  $('#mimoBox').classList.toggle('hide', !on);
  if (!on) {
    // 非小米服务商：音色 ID 一定可见（别被小米克隆模式隐藏后带过去）
    const vRow0 = $('#ttsVoiceRow');
    if (vRow0) vRow0.classList.remove('hide');
    return;
  }

  const mode = S.tts.mimoMode || 'preset';
  const mSel = $('#mimoMode');
  if (!mSel.dataset.filled && mimoModes.length) {
    mSel.innerHTML = mimoModes.map((m) =>
      `<option value="${escapeHtml(m.key)}">${escapeHtml(m.label)}</option>`).join('');
    mSel.dataset.filled = '1';
  }
  mSel.value = mode;
  const mInfo = mimoModes.find((m) => m.key === mode) || {};
  $('#mimoModeHint').textContent = mInfo.hint
    ? `${mInfo.hint}　（模型 ${mInfo.model || ''}）` : '';

  $('#mimoPresetRow').classList.toggle('hide', mode !== 'preset');
  $('#mimoDesignRow').classList.toggle('hide', mode !== 'design');
  $('#mimoCloneRow').classList.toggle('hide', mode !== 'clone');
  // 音色 ID 只对「预置音色」有意义：设计和克隆都不用它
  const vRow = $('#ttsVoiceRow');
  if (vRow) vRow.classList.toggle('hide', mode !== 'preset');

  const sel = $('#mimoVoiceSel');
  if (!sel.dataset.filled && mimoVoices.length) {
    sel.innerHTML = mimoVoices.map((v) => {
      const meta = [v.lang, v.gender].filter(Boolean).join(' · ');
      return `<option value="${escapeHtml(v.id)}">${escapeHtml(v.name)}${meta ? '（' + escapeHtml(meta) + '）' : ''}</option>`;
    }).join('');
    sel.dataset.filled = '1';
  }
  const want = S.tts.voice || 'mimo_default';
  sel.value = want;
  if (!sel.value) sel.value = 'mimo_default';

  const desc = $('#mimoVoiceDesc');
  desc.value = S.tts.mimoVoiceDesc || '';
  desc.placeholder = '例：' + (mimoDesignDefault || '五十多岁的男性评书先生，北方口音，嗓音醇厚略带沙哑');

  fillCloneList();
  $('#mimoStyle').value = S.tts.style || '';
  $('#mimoKeyState').textContent = mimoReady
    ? ('✓ 已配置你的 Key' + (mimoKeyMasked ? '：' + mimoKeyMasked : '') + '（加密存服务端，前端只显示掩码）')
    : '⚠ 还没配置你的 Key：粘贴到上面输入框，点「保存」写入（只有你能用）';
}

/* 克隆样本下拉：只有名字 + 大小，音频本身留在服务端 */
function fillCloneList() {
  const sel = $('#mimoCloneSel');
  if (!sel) return;
  if (!mimoClones.length) {
    sel.innerHTML = '<option value="">（还没有样本，先上传一段 mp3/wav）</option>';
    sel.value = '';
    return;
  }
  sel.innerHTML = mimoClones.map((c) =>
    `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)}（${fmtBytes(c.bytes)}）</option>`).join('');
  const want = S.tts.mimoClone || '';
  sel.value = want;
  if (!sel.value) {
    sel.value = mimoClones[0].name;
    S.tts.mimoClone = sel.value;
  }
}

function applyPreset(key) {
  const p = presets[key];
  if (!p) return;
  // 每个服务商各存一份：切过去先看有没有存过（Fish 的 key、音色不会因为切走而丢）
  const saved = (S.tts.profiles || {})[key];
  const src = Object.assign({}, p, saved || {});
  S.tts.url = src.url || '';
  S.tts.method = src.method || 'POST';
  S.tts.headers = src.headers || '';
  S.tts.body = src.body || '';
  S.tts.proxy = src.proxy != null ? src.proxy : '';
  S.tts.provider = src.provider || '';
  S.tts.model = src.model || '';
  S.tts.style = src.style || '';
  S.tts.clientSpeed = !!src.clientSpeed;
  S.tts.voice = src.voice || '';
  S.tts.mimoMode = src.mimoMode || 'preset';
  S.tts.mimoVoiceDesc = src.mimoVoiceDesc || '';
  S.tts.mimoClone = src.mimoClone || '';
  if (saved) {
    if (saved.speed) S.tts.speed = saved.speed;
  } else if (p.speed && !p.keepSpeed) {
    S.tts.speed = p.speed;
  }
  S.tts.engine = key === 'web' ? 'web' : 'http';
  fillSetForm();
  $('#presetNote').textContent = p.note || '';
  saveSettings();
}

/* ---------------- 书源搜索与书籍详情 ---------------- */
let srcIndex = null;
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
  sourcesList = r.sources || [];
  for (const s of sourcesList) srcIndex[s.name] = s.id;
  return srcIndex;
}

async function doSearch() {
  const kw = $('#searchInput').value.trim();
  if (!kw) return;
  const box = $('#searchRes');
  box.innerHTML = '<p class="hint" style="padding:18px">正在并发检索各路书源，请稍候…</p>';
  $('#searchStat').textContent = '';
  try {
    const r = await api('/api/src/search?q=' + encodeURIComponent(kw));
    const st = r.stats || {};
    $('#searchStat').textContent =
      `⚡ 命中 ${st.ok}/${st.sources} 个源 · 发现 ${st.books} 本 · 耗时 ${((st.ms || 0) / 1000).toFixed(1)}s`;
    box.innerHTML = '';
    const groups = (r.results || []).filter((g) => g.books.length);
    if (!groups.length) {
      box.innerHTML = '<p class="hint" style="padding:18px">未检索到相关书籍。可尝试更换关键词，或在「书源」中检查可用书源。</p>';
      return;
    }
    for (const g of groups) {
      const h = document.createElement('div');
      h.className = 'res-group';
      h.innerHTML = `<b>${escapeHtml(g.source)}</b> <span>${g.books.length} 本</span>`;
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
    box.innerHTML = `<p class="hint" style="padding:18px;color:var(--err)">搜索失败：${escapeHtml(e.message)}</p>`;
  }
}

async function openDetail(sourceName, b) {
  detailBook = b;
  showView('detail');
  $('#detailTitle').textContent = b.name;
  $('#detailBody').innerHTML = '<p class="hint" style="padding:20px">正在获取书籍详情与最新目录，约需 2–8 秒…</p>';
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
        <div class="line">作者：${escapeHtml(author || '佚名')}</div>
        ${kind ? `<div class="line">类型：${escapeHtml(kind)}</div>` : ''}
        ${info.lastChapter ? `<div class="line">最新：${escapeHtml(info.lastChapter)}</div>` : ''}
        <div class="line">${chs.length ? '共 ' + chs.length + ' 章' : (toc.error ? '目录获取失败' : '暂无目录')}</div>
      </div>
    </div>
    ${intro ? `<div class="d-intro">${escapeHtml(intro)}</div>` : ''}
    <div class="d-actions">
      <button class="btn primary" id="btnAddShelf">
        <svg class="svg-icon"><use href="#icon-plus"></use></svg>
        <span>加入书架</span>
      </button>
      ${chs.length ? '<button class="btn secondary" id="btnPreview">试读第一章</button>' : ''}
    </div>
    ${toc.error ? `<p class="hint" style="color:var(--err)">目录解析出错：${escapeHtml(toc.error)}</p>` : ''}
    ${chs.length ? `<div class="hint" style="font-weight:600;margin:12px 0 8px">目录速览（展示前 200 章）：</div>
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
  toast('正在抓取正文…', '', 30000);
  try {
    const r = await api(`/api/src/ch?sid=${detailSid}&url=${encodeURIComponent(c.url)}&book=${encodeURIComponent(detailBook.url)}`);
    $('#toast').classList.add('hide');
    if (r.error) { toast('抓取正文失败：' + r.error, 'err', 4000); return; }
    openTextPreview(c.name || detailBook.name, r.content || '');
  } catch (e) { toast('抓取失败：' + e.message, 'err', 4000); }
}

function openTextPreview(title, text) {
  book = { id: -1, title, chapters: [{ idx: 0, title }] };
  chIdx = 0; chCount = 1;
  previewing = true;
  $('#bookTitle').textContent = title;
  const sub = $('#chapterSubTitle'); if (sub) sub.textContent = '试读模式';
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
  btn.textContent = '正在获取章节数据…';
  try {
    const r = await api('/api/src/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sid: detailSid,
        book: detailBook,
        tocUrl: '',
        chapters: detailToc.length ? detailToc : undefined,
      }),
    });
    toast(`《${detailBook.name}》已加入书架 (${r.chapters} 章)`, 'ok');
    await loadShelf();
    showView('shelf');
    setTimeout(() => openBook(r.id), 350);
  } catch (e) {
    toast('加入书架失败：' + e.message, 'err', 4000);
    btn.disabled = false;
    btn.textContent = '加入书架';
  }
}

/* ---------------- 书源管理 ---------------- */
async function openSources() {
  showView('srcmgr');
  const box = $('#srcList');
  box.innerHTML = '<p class="hint" style="padding:16px">正在加载书源列表…</p>';
  try {
    const r = await api('/api/sources');
    sourcesList = r.sources || [];
    renderSourcesList(sourcesList);
  } catch (e) {
    box.innerHTML = `<p class="hint" style="padding:16px;color:var(--err)">加载失败：${escapeHtml(e.message)}</p>`;
  }
}

function renderSourcesList(list) {
  const box = $('#srcList');
  box.innerHTML = '';
  curSrcList = list;
  const enabledCount = list.filter((s) => s.enabled).length;
  $('#srcStat').textContent = `共 ${list.length} 个书源 (已启用 ${enabledCount} 个)`;

  srcIndex = {};
  for (const s of list) {
    srcIndex[s.name] = s.id;
    const row = document.createElement('div');
    row.className = 'src-row' + (srcMulti ? ' multi' : '') + (srcSel.has(s.id) ? ' picked' : '');
    row.innerHTML = `
      ${srcMulti ? `<button class="src-check ${srcSel.has(s.id) ? 'on' : ''}" title="选中"></button>` : ''}
      <button class="sw ${s.enabled ? 'on' : ''}" title="点击启用/停用此书源"></button>
      <div class="n">
        <b>${escapeHtml(s.name)}</b>
        <span>${escapeHtml((s.url || '').replace(/^https?:\/\//, '').slice(0, 42))}</span>
      </div>
      ${srcMulti ? '' : '<button class="mini" title="单源并发测试">测试</button><button class="mini danger" title="删除此书源">删除</button>'}`;

    if (srcMulti) {
      row.onclick = (ev) => {
        if (ev.target.closest('.sw')) return;
        if (srcSel.has(s.id)) srcSel.delete(s.id);
        else srcSel.add(s.id);
        row.classList.toggle('picked', srcSel.has(s.id));
        const ck = row.querySelector('.src-check');
        if (ck) ck.classList.toggle('on', srcSel.has(s.id));
        updateBulkBar();
      };
    }

    row.querySelector('.sw').onclick = async (ev) => {
      const b = ev.currentTarget;
      try {
        const res = await api(`/api/sources/${s.id}/toggle`, { method: 'POST' });
        b.classList.toggle('on', res.enabled);
        s.enabled = res.enabled;
        const enCount = sourcesList.filter((x) => x.enabled).length;
        $('#srcStat').textContent = `共 ${sourcesList.length} 个书源 (已启用 ${enCount} 个)`;
      } catch (err) {
        toast('操作失败：' + err.message, 'err');
      }
    };

    const testBtn = row.querySelector('.mini:not(.danger)');
    if (testBtn) testBtn.onclick = async (ev) => {
      const b = ev.currentTarget;
      b.disabled = true; b.textContent = '测试中…';
      try {
        const t = await api(`/api/sources/${s.id}/test`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: $('#searchInput') ? $('#searchInput').value.trim() || '剑来' : '剑来' }),
        });
        const n = t.count != null ? t.count : (t.books || []).length;
        b.textContent = n ? n + ' 本' : '无结果';
        toast(n ? `${s.name}：命中 ${n} 本书` : `${s.name}：${t.error || '未搜到内容'}`, n ? 'ok' : 'err', 3500);
      } catch (e) {
        b.textContent = '失败';
        toast(s.name + '：' + e.message, 'err', 3500);
      }
      setTimeout(() => { b.disabled = false; b.textContent = '测试'; }, 2500);
    };

    const delBtn = row.querySelector('.mini.danger');
    if (delBtn) delBtn.onclick = () => {
      showConfirm({
        title: '删除书源',
        msg: `确定删除「${s.name}」吗？删掉后要重新导入才能恢复。`,
        okText: '删除',
        onOk: async () => {
          try {
            await api(`/api/sources/${s.id}`, { method: 'DELETE' });
            sourcesList = sourcesList.filter((x) => x.id !== s.id);
            srcSel.delete(s.id);
            filterSources($('#srcFilter').value);
            updateBulkBar();
            toast(`已删除「${s.name}」`, 'ok');
          } catch (e) {
            toast('删除失败：' + e.message, 'err', 4000);
          }
        },
      });
    };
    box.appendChild(row);
  }
}

/* ---------------- 书源多选 / 批量删除 ---------------- */
function setSrcMulti(on) {
  srcMulti = on;
  srcSel.clear();
  const btn = $('#btnSrcMulti');
  btn.classList.toggle('primary', on);
  btn.classList.toggle('ghost', !on);
  btn.querySelector('span').textContent = on ? '完成' : '多选';
  $('#srcBulkBar').classList.toggle('hide', !on);
  filterSources($('#srcFilter').value);
  updateBulkBar();
}

function updateBulkBar() {
  if (!srcMulti) return;
  const n = srcSel.size;
  $('#srcBulkInfo').textContent = `已选 ${n} 个${n ? ' · 共 ' + curSrcList.length + ' 个可见' : ''}`;
  $('#btnSrcDelSel').disabled = n === 0;
}

function deleteSelectedSources() {
  const ids = [...srcSel];
  if (!ids.length) return;
  const sample = curSrcList.filter((s) => srcSel.has(s.id)).slice(0, 3).map((s) => s.name).join('、');
  showConfirm({
    title: `删除 ${ids.length} 个书源`,
    msg: `将删除：${sample}${ids.length > 3 ? ` 等 ${ids.length} 个` : ''}。删掉后要重新导入才能恢复。`,
    okText: '删除',
    onOk: async () => {
      let done = 0;
      try {
        const r = await api('/api/sources/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids }),
        });
        done = r.deleted || 0;
      } catch (e) {
        toast('批量删除失败：' + e.message, 'err', 4000);
        return;
      }
      sourcesList = sourcesList.filter((x) => !srcSel.has(x.id));
      srcSel.clear();
      filterSources($('#srcFilter').value);
      updateBulkBar();
      toast(`已删除 ${done} 个书源`, 'ok', 3000);
    },
  });
}

function filterSources(kw) {
  kw = (kw || '').trim().toLowerCase();
  if (!kw) {
    renderSourcesList(sourcesList);
    return;
  }
  const filtered = sourcesList.filter((s) =>
    (s.name || '').toLowerCase().includes(kw) || (s.url || '').toLowerCase().includes(kw)
  );
  renderSourcesList(filtered);
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
  toast('正在导入书源，请稍候…', '', 30000);
  try {
    const r = await api('/api/sources/import', { method: 'POST', body: f });
    toast(`导入书源成功：新增 ${r.added} 个，更新 ${r.updated} 个，共 ${r.total} 个`, 'ok', 4000);
    srcIndex = null;
    openSources();
  } catch (e) {
    toast('导入书源失败：' + e.message, 'err', 4000);
  }
}

/* ---------------- 添加书源面板 ---------------- */
function openSrcAdd() {
  $('#srcAddText').value = '';
  $('#srcAddUrl').value = '';
  $('#srcAddMsg').textContent = '';
  $('#srcAddMsg').style.color = '';
  const btn = $('#btnSrcAddSave');
  btn.disabled = false;
  btn.textContent = '添加';
  $('#srcAddPanel').classList.remove('hide');
  $('#mask').classList.remove('hide');
  setTimeout(() => $('#srcAddText').focus(), 120);
}

function closeSrcAdd() {
  $('#srcAddPanel').classList.add('hide');
  if ($('#setPanel').classList.contains('hide') && $('#ttsPanel').classList.contains('hide')) {
    $('#mask').classList.add('hide');
  }
}

function srcAddMsg(text, isErr) {
  const el = $('#srcAddMsg');
  el.textContent = text;
  el.style.color = isErr ? 'var(--err)' : '';
}

async function submitSrcAdd() {
  const url = $('#srcAddUrl').value.trim();
  const text = $('#srcAddText').value.trim();
  if (!url && !text) {
    srcAddMsg('请粘贴书源 JSON，或填写一个书源链接', true);
    return;
  }
  const btn = $('#btnSrcAddSave');
  btn.disabled = true;
  btn.textContent = '添加中…';
  srcAddMsg('正在添加…');
  try {
    const r = url
      ? await api('/api/sources/fetch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url }),
        })
      : await api('/api/sources/import', { method: 'POST', body: text });
    toast(`添加成功：新增 ${r.added} 个，更新 ${r.updated} 个，共 ${r.total} 个`, 'ok', 4000);
    closeSrcAdd();
    srcIndex = null;
    openSources();
  } catch (e) {
    srcAddMsg('添加失败：' + e.message, true);
    btn.disabled = false;
    btn.textContent = '添加';
  }
}

/* ---------------- 事件监听绑定 ---------------- */
function bind() {
  // 书架筛选与导入
  const shelfFilter = $('#shelfFilter');
  if (shelfFilter) {
    shelfFilter.oninput = (e) => filterShelf(e.target.value);
  }
  $('#btnImport').onclick = () => $('#fileInput').click();
  const emptyImport = $('#btnEmptyImport');
  if (emptyImport) emptyImport.onclick = () => $('#fileInput').click();
  const emptyDisc = $('#btnEmptyDiscover');
  if (emptyDisc) emptyDisc.onclick = () => { showView('discover'); $('#searchInput').focus(); };

  // 拖拽导入支持
  const dropzone = $('#emptyDropzone');
  const shelfMain = $('#shelfMain');
  const handleDrag = (e) => { e.preventDefault(); e.stopPropagation(); };
  ['dragenter', 'dragover'].forEach((n) => {
    window.addEventListener(n, handleDrag);
    if (dropzone) dropzone.addEventListener(n, () => dropzone.classList.add('dragover'));
  });
  ['dragleave', 'drop'].forEach((n) => {
    window.addEventListener(n, handleDrag);
    if (dropzone) dropzone.addEventListener(n, () => dropzone.classList.remove('dragover'));
  });
  window.addEventListener('drop', (e) => {
    handleDrag(e);
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
      importFiles([...e.dataTransfer.files]);
    }
  });

  // 书源页面事件
  $('#btnSrcExportOn').onclick = () => exportSources(true);
  $('#btnSrcExportAll').onclick = () => exportSources(false);
  $('#btnSrcAdd').onclick = openSrcAdd;
  $('#btnSrcAddClose').onclick = closeSrcAdd;
  $('#btnSrcAddCancel').onclick = closeSrcAdd;
  $('#btnSrcAddSave').onclick = submitSrcAdd;
  $('#btnSrcMulti').onclick = () => setSrcMulti(!srcMulti);
  $('#btnSrcMultiExit').onclick = () => setSrcMulti(false);
  $('#btnSrcSelAll').onclick = () => {
    curSrcList.forEach((s) => srcSel.add(s.id));
    filterSources($('#srcFilter').value);
    updateBulkBar();
  };
  $('#btnSrcSelNone').onclick = () => {
    srcSel.clear();
    filterSources($('#srcFilter').value);
    updateBulkBar();
  };
  $('#btnSrcDelSel').onclick = deleteSelectedSources;
  $('#btnDiscover').onclick = () => { showView('discover'); $('#searchInput').focus(); };
  $('#btnDiscBack').onclick = () => showView('shelf');
  $('#btnDoSearch').onclick = doSearch;
  $('#searchInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });
  
  const searchClear = $('#btnSearchClear');
  const searchInput = $('#searchInput');
  if (searchClear && searchInput) {
    searchInput.oninput = () => searchClear.classList.toggle('hide', !searchInput.value);
    searchClear.onclick = () => { searchInput.value = ''; searchClear.classList.add('hide'); searchInput.focus(); };
  }

  $('#btnDetailBack').onclick = () => showView('discover');
  $('#btnSources').onclick = openSources;
  $('#btnSrcBack').onclick = () => showView('shelf');
  $('#btnSrcImport').onclick = () => $('#srcFile').click();
  $('#srcFile').onchange = (e) => { const f = e.target.files[0]; if (f) importSourceFile(f); e.target.value = ''; };
  $('#fileInput').onchange = (e) => { importFiles([...e.target.files]); e.target.value = ''; };

  const srcFilter = $('#srcFilter');
  if (srcFilter) srcFilter.oninput = (e) => filterSources(e.target.value);

  const srcToggleAll = $('#btnSrcToggleAll');
  if (srcToggleAll) {
    srcToggleAll.onclick = async () => {
      const anyOff = sourcesList.some((s) => !s.enabled);
      toast(anyOff ? '正在批量开启所有源…' : '正在批量停用所有源…');
      for (const s of sourcesList) {
        if (s.enabled !== anyOff) {
          try {
            const res = await api(`/api/sources/${s.id}/toggle`, { method: 'POST' });
            s.enabled = res.enabled;
          } catch (e) {}
        }
      }
      renderSourcesList(sourcesList);
      toast('批量设置完成', 'ok');
    };
  }

  // 设置面板触发
  $('#btnShelfSet').onclick = () => openSet('read');
  $('#btnBack').onclick = () => { stopSpeak(); hideCapsule(); showView('shelf'); loadShelf(); };
  $('#btnToc').onclick = openDrawer;
  $('#btnTocClose').onclick = closeDrawer;
  const btnSetClose = $('#btnSetPanelClose');
  if (btnSetClose) btnSetClose.onclick = closeSet;
  const btnTtsClose = $('#btnTtsPanelClose');
  if (btnTtsClose) btnTtsClose.onclick = () => setTtsPanel(false);

  $('#mask').onclick = () => { closeDrawer(); closeSet(); setTtsPanel(false); closeSrcAdd(); };
  $('#btnReaderSet').onclick = () => openSet('read');
  $('#btnPrev').onclick = () => loadChapter(chIdx - 1, 0);
  $('#btnNext').onclick = () => loadChapter(chIdx + 1, 0);

  // 滚动事件处理
  let scrollRaf = 0;
  $('#content').addEventListener('scroll', () => {
    if (scrollRaf) return;
    scrollRaf = requestAnimationFrame(() => {
      scrollRaf = 0;
      updateProgress();
      ensureAhead();
    });
  }, { passive: true });

  // 触控交互与翻页
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

  // 点击正文处理
  $('#content').addEventListener('click', (e) => {
    if (swiped) { swiped = false; return; }
    if (e.target.classList.contains('s')) {
      const i = +e.target.dataset.i;
      speaking = true;
      updatePlayButtonUI(true);
      showCapsule();
      startFrom(unitOfSpan(i));
      return;
    }
    const sel = window.getSelection();
    if (sel && sel.toString().length > 0) return;

    if (S.pageMode !== 'scroll') {
      const r = $('#content').getBoundingClientRect();
      const x = (e.clientX - r.left) / (r.width || 1);
      if (x > 0.72) { turnPage(1); return; }
      if (x < 0.28) { turnPage(-1); return; }
    }
    // 中间区域点击：优雅切换顶部与底部的沉浸式模式
    document.body.classList.toggle('hud-hidden');
  });

  // 听书与胶囊交互
  $('#btnSpeak').onclick = () => {
    if (!speaking && $('#ttsCapsule').classList.contains('hide')) showCapsule();
    toggleSpeak();
  };
  $('#ttsPlay').onclick = toggleSpeak;
  $('#ttsPrev').onclick = () => { speaking = true; updatePlayButtonUI(true); startFrom(Math.max(0, curIdx - 1)); };
  $('#ttsNext').onclick = () => { speaking = true; updatePlayButtonUI(true); startFrom(curIdx + 1); };

  // 灵动胶囊控制器绑定
  $('#capsulePlay').onclick = toggleSpeak;
  $('#capsulePrev').onclick = () => { speaking = true; updatePlayButtonUI(true); startFrom(Math.max(0, curIdx - 1)); };
  $('#capsuleNext').onclick = () => { speaking = true; updatePlayButtonUI(true); startFrom(curIdx + 1); };
  $('#capsuleClose').onclick = () => { stopSpeak(); hideCapsule(); };
  $('#capsuleExpand').onclick = () => setTtsPanel(true);
  $('#capsuleInfo').onclick = () => setTtsPanel(true);

  // 语速快捷轮转
  const SPEEDS = [0.8, 1.0, 1.18, 1.5, 1.8, 2.0];
  $('#capsuleSpeedBtn').onclick = () => {
    const cur = +S.tts.speed || 1.18;
    let next = SPEEDS.find((s) => s > cur + 0.05);
    if (!next) next = SPEEDS[0];
    S.tts.speed = next;
    $('#speedVal').textContent = next.toFixed(2);
    $('#ttsSpeed').value = next;
    $('#capsuleSpeedText').textContent = next.toFixed(2) + 'x';
    if (S.tts.clientSpeed) applyPlaybackRate();
    saveSettings();
    toast(`语速调整为 ${next.toFixed(2)}x`);
  };

  $('#ttsSpeed').oninput = (e) => {
    S.tts.speed = +e.target.value;
    $('#speedVal').textContent = S.tts.speed.toFixed(2);
    $('#capsuleSpeedText').textContent = S.tts.speed.toFixed(2) + 'x';
    if (S.tts.clientSpeed) applyPlaybackRate();   // 小米：正在播的这句立刻变速
    saveSettings();
  };

  // 语速预设胶囊按钮
  $$('.speed-pill').forEach((btn) => {
    btn.onclick = () => {
      const sp = +btn.dataset.speed;
      S.tts.speed = sp;
      $('#speedVal').textContent = sp.toFixed(2);
      $('#ttsSpeed').value = sp;
      $('#capsuleSpeedText').textContent = sp.toFixed(2) + 'x';
      $$('.speed-pill').forEach((x) => x.classList.toggle('active', x === btn));
      if (S.tts.clientSpeed) applyPlaybackRate();
      saveSettings();
    };
  });

  $('#ttsEngine').onchange = (e) => {
    S.tts.engine = e.target.value;
    $('#webVoiceRow').classList.toggle('hide', S.tts.engine !== 'web');
    saveSettings();
  };
  // 选预设 → 套用该服务商配置（原来这个下拉没绑事件，选了没反应）
  $('#ttsPreset').onchange = (e) => applyPreset(e.target.value);
  // 选小米预置音色 → 填进「音色 ID」，手填仍可覆盖
  $('#mimoVoiceSel').onchange = (e) => { $('#ttsVoice').value = e.target.value; };

  // 小米音色来源：预置 / 音色设计 / 音色克隆
  $('#mimoMode').onchange = (e) => {
    S.tts.mimoMode = e.target.value || 'preset';
    fillMimoForm();
    saveSettings();
  };
  $('#mimoVoiceDesc').oninput = (e) => { S.tts.mimoVoiceDesc = e.target.value.trim(); };
  $('#mimoCloneSel').onchange = (e) => {
    S.tts.mimoClone = e.target.value || '';
    saveSettings();
  };
  // 上传克隆样本：走服务端中转，音频存 data/mimo_clones，前端只拿名字
  $('#btnMimoCloneUp').onclick = async () => {
    const f = $('#mimoCloneFile').files[0];
    if (!f) return toast('先选一个 mp3/wav 文件', 'err', 3000);
    if (f.size > mimoCloneMax) {
      return toast(`样本太大（${fmtBytes(f.size)}），上限 ${fmtBytes(mimoCloneMax)}`, 'err', 4000);
    }
    const name = (prompt('给这段样本起个名字（用来在音色克隆里选）', f.name.replace(/\.[^.]+$/, '')) || '').trim();
    if (!name) return;
    const btn = $('#btnMimoCloneUp');
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '上传中…';
    try {
      const data = await new Promise((res, rej) => {
        const fr = new FileReader();
        fr.onload = () => res(String(fr.result).split(',')[1] || '');
        fr.onerror = () => rej(new Error('读取文件失败'));
        fr.readAsDataURL(f);
      });
      const r = await api('/api/tts/mimo/clone', {
        method: 'POST',
        body: JSON.stringify({ name, filename: f.name, data })
      });
      mimoClones = r.clones || [];
      S.tts.mimoClone = name;
      S.tts.mimoMode = 'clone';
      fillMimoForm();
      await saveSettings();
      toast(`样本「${name}」已存到服务端`, 'ok');
    } catch (e) {
      toast('上传失败：' + e.message, 'err', 4000);
    } finally {
      btn.disabled = false;
      btn.textContent = old;
      $('#mimoCloneFile').value = '';
    }
  };
  $('#btnMimoCloneDel').onclick = async () => {
    const name = $('#mimoCloneSel').value;
    if (!name) return toast('没有可删的样本', 'err', 3000);
    if (!confirm(`删除克隆样本「${name}」？`)) return;
    try {
      const r = await api('/api/tts/mimo/clone/delete', {
        method: 'POST', body: JSON.stringify({ name })
      });
      mimoClones = r.clones || [];
      if (S.tts.mimoClone === name) S.tts.mimoClone = mimoClones[0] ? mimoClones[0].name : '';
      fillMimoForm();
      await saveSettings();
      toast('已删除', 'ok');
    } catch (e) {
      toast('删除失败：' + e.message, 'err', 4000);
    }
  };
  $('#btnTtsCfg').onclick = () => { setTtsPanel(false); openSet('tts'); };

  // 排版步进器
  $('#btnFsDec').onclick = () => {
    S.fontSize = Math.max(14, S.fontSize - 1);
    applyStyle();
    saveSettings();
  };
  $('#btnFsInc').onclick = () => {
    S.fontSize = Math.min(34, S.fontSize + 1);
    applyStyle();
    saveSettings();
  };
  $('#setFontSize').oninput = (e) => { S.fontSize = +e.target.value; applyStyle(); saveSettings(); };
  $('#setLineHeight').oninput = (e) => { S.lineHeight = +e.target.value; applyStyle(); saveSettings(); };
  $('#setPadding').oninput = (e) => { S.padding = +e.target.value; applyStyle(); saveSettings(); };
  $('#setFont').onchange = (e) => { S.font = e.target.value; applyStyle(); saveSettings(); };

  $$('#pageModes button').forEach((b) => b.onclick = () => {
    S.pageMode = b.dataset.pm;
    applyPageMode();
    requestAnimationFrame(() => { scrollToChapterTop(); ensureAhead(); });
    saveSettings();
    toast('已切换翻页方式：' + b.querySelector('.seg-title').textContent);
  });

  $('#ttsAutoNext').onchange = (e) => { S.tts.autoNext = e.target.checked; saveSettings(); };
  $('#autoCh').onchange = (e) => {
    S.autoCh = e.target.checked;
    saveSettings();
    if (S.autoCh) ensureAhead();
  };

  $$('#themes .theme-card').forEach((b) => b.onclick = () => {
    S.theme = b.dataset.theme;
    applyStyle();
    saveSettings();
  });

  $$('.tab').forEach((b) => b.onclick = () => switchTab(b.dataset.tab));

  $('#btnSaveSet').onclick = async () => {
    S.tts.url = $('#ttsUrl').value.trim();
    S.tts.method = $('#ttsMethod').value;
    S.tts.headers = $('#ttsHeaders').value;
    S.tts.body = $('#ttsBody').value;
    S.tts.voice = $('#ttsVoice').value.trim();
    S.tts.proxy = $('#ttsProxy').value.trim();
    if (S.tts.provider === 'mimo') {
      S.tts.style = $('#mimoStyle').value.trim();
      S.tts.mimoMode = $('#mimoMode').value || 'preset';
      S.tts.mimoVoiceDesc = $('#mimoVoiceDesc').value.trim();
      S.tts.mimoClone = $('#mimoCloneSel').value || '';
      if (S.tts.mimoMode === 'clone' && !S.tts.mimoClone) {
        return toast('音色克隆要先上传一段样本', 'err', 3500);
      }
      if (S.tts.mimoMode === 'design' && !S.tts.mimoVoiceDesc) {
        return toast('音色设计要写一句音色描述', 'err', 3500);
      }
      const k = $('#mimoKeyInput').value.trim();
      if (k) {
        try {
          const r = await api('/api/tts/key', { method: 'POST', body: JSON.stringify({ key: k }) });
          mimoReady = !!r.ready;
          $('#mimoKeyInput').value = '';
          mimoKeyMasked = r.masked || '';
          $('#mimoKeyState').textContent = '✓ 已配置你的 Key' + (mimoKeyMasked ? '：' + mimoKeyMasked : '');
          toast('小米 Key 已写入服务端', 'ok');
        } catch (e) {
          toast('Key 保存失败：' + e.message, 'err', 4000);
        }
      }
    }
    const pk = currentPresetKey();
    if (pk) {
      S.tts.profiles = Object.assign({}, S.tts.profiles || {});
      S.tts.profiles[pk] = {
        url: S.tts.url, method: S.tts.method, headers: S.tts.headers, body: S.tts.body,
        voice: S.tts.voice, proxy: S.tts.proxy, provider: S.tts.provider,
        model: S.tts.model, style: S.tts.style, clientSpeed: S.tts.clientSpeed,
        mimoMode: S.tts.mimoMode, mimoVoiceDesc: S.tts.mimoVoiceDesc, mimoClone: S.tts.mimoClone,
        speed: S.tts.speed, engine: S.tts.engine
      };
    }
    await saveSettings();
    audioCache.clear();
    applyPlaybackRate();
    toast('已保存', 'ok');
    closeSet();
  };

  $('#btnTestTts').onclick = async () => {
    const isMimo = S.tts.provider === 'mimo';
    const cfg = {
      url: $('#ttsUrl').value.trim(),
      method: $('#ttsMethod').value,
      headers: $('#ttsHeaders').value,
      body: $('#ttsBody').value,
      proxy: $('#ttsProxy').value.trim(),
      engine: 'http',
      provider: S.tts.provider || '',
      model: S.tts.model || '',
      style: isMimo ? $('#mimoStyle').value.trim() : '',
      mimoMode: isMimo ? ($('#mimoMode').value || 'preset') : '',
      mimoVoiceDesc: isMimo ? $('#mimoVoiceDesc').value.trim() : '',
      mimoClone: isMimo ? ($('#mimoCloneSel').value || '') : ''
    };
    const voice = $('#ttsVoice').value.trim();
    const box = $('#testResult');
    box.classList.add('show');
    box.innerHTML = '<span style="color:var(--dim)">正在连接服务合成语音测试…</span>';

    try {
      const r = await api('/api/tts/test', {
        method: 'POST',
        body: JSON.stringify({ text: '你好，这是一次朗读测试。', voice, speed: +S.tts.speed, cfg })
      });
      if (r.ok) {
        box.innerHTML = `<span style="color:var(--ok);font-weight:600">✓ 测试成功</span> · 大小: ${fmtBytes(r.bytes)} · 耗时: ${r.ms}ms · 类型: ${r.content_type}`;
        S.tts = { ...S.tts, ...cfg, voice };
        await saveSettings();
        const resp = await fetch('/api/tts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            text: '你好，这是一次朗读测试。', voice, speed: +S.tts.speed, cache: false,
            provider: S.tts.provider || '', model: S.tts.model || '', style: S.tts.style || '',
            mimoMode: cfg.mimoMode, mimoVoiceDesc: cfg.mimoVoiceDesc, mimoClone: cfg.mimoClone
          })
        });
        if (resp.ok) {
          audioEl.src = URL.createObjectURL(await resp.blob());
          applyPlaybackRate();
          audioEl.play();
        }
      } else {
        box.innerHTML = `<span style="color:var(--err);font-weight:600">✗ ${escapeHtml(r.error || '测试失败')}</span>` +
          (r.status ? ` (HTTP ${r.status})` : '') +
          (r.body ? `<br><code style="margin-top:4px;display:block">${escapeHtml(String(r.body).slice(0, 180))}</code>` : '');
      }
    } catch (e) {
      box.innerHTML = `<span style="color:var(--err)">✗ 接口调用失败：${escapeHtml(e.message)}</span>`;
    }
  };

  // 目录内搜索
  let st = null;
  const tocSearch = $('#tocSearch');
  const tocClear = $('#btnTocClear');
  if (tocSearch && tocClear) {
    tocSearch.oninput = (e) => {
      clearTimeout(st);
      const kw = e.target.value.trim();
      tocClear.classList.toggle('hide', !kw);
      if (!kw) {
        $('#searchHits').classList.add('hide');
        $('#tocList').classList.remove('hide');
        return;
      }
      st = setTimeout(async () => {
        try {
          const { hits } = await api(`/api/books/${book.id}/search?q=${encodeURIComponent(kw)}`);
          const box = $('#searchHits');
          box.innerHTML = hits.length ? '' : '<p class="hint" style="padding:16px;text-align:center">未找到匹配内容</p>';
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
        } catch (err) {}
      }, 350);
    };

    tocClear.onclick = () => {
      tocSearch.value = '';
      tocClear.classList.add('hide');
      $('#searchHits').classList.add('hide');
      $('#tocList').classList.remove('hide');
    };
  }

  const btnScrollCur = $('#btnTocScrollCur');
  if (btnScrollCur) {
    btnScrollCur.onclick = () => {
      const cur = $('#tocList .cur');
      if (cur) cur.scrollIntoView({ block: 'center', behavior: 'smooth' });
    };
  }

  // 系统自带语音识别加载
  const fillVoices = () => {
    const sel = $('#webVoice');
    if (!sel || !('speechSynthesis' in window)) return;
    sel.innerHTML = speechSynthesis.getVoices()
      .map((v) => `<option value="${v.voiceURI}">${escapeHtml(v.name)} (${v.lang})</option>`).join('');
  };
  if ('speechSynthesis' in window) {
    speechSynthesis.onvoiceschanged = fillVoices;
    fillVoices();
  }

  // 全局快捷键
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeDrawer(); closeSet(); setTtsPanel(false);
      $('#confirmModal').classList.add('hide');
      return;
    }
    if ($('#reader').classList.contains('hide')) return;
    if (e.key === 'PageDown') { e.preventDefault(); loadChapter(chIdx + 1, 0); return; }
    if (e.key === 'PageUp') { e.preventDefault(); loadChapter(chIdx - 1, 0); return; }
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      const d = e.key === 'ArrowRight' ? 1 : -1;
      if (S.pageMode === 'scroll') loadChapter(chIdx + d, 0);
      else turnPage(d);
      return;
    }
    if (e.key === ' ') {
      // 避免输入框按空格触发听书
      if (['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) return;
      e.preventDefault();
      toggleSpeak();
      return;
    }
    if (e.key === 'm' || e.key === 'M') {
      if (['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) return;
      $('#tocDrawer').classList.contains('hide') ? openDrawer() : closeDrawer();
    }
  });
}

/* 顶栏账号：显示首字，点击退出 */
async function loadMe() {
  try {
    const me = await api('/api/auth/me');
    const name = (me && me.user && me.user.username) || '';
    if (!name) return;
    const av = $('#userAvatar');
    if (av) av.textContent = name.slice(0, 1).toUpperCase();
    const b = $('#btnUser');
    if (!b) return;
    b.title = '当前账号：' + name + '（点击退出登录）';
    b.onclick = async () => {
      if (!confirm('退出登录？')) return;
      try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) { }
      location.replace('/login');
    };
  } catch (e) { }
}

/* ---------------- 初始化运行 ---------------- */
(async function init() {
  bind();
  await loadMe();
  await loadSettings();
  applyPageMode();
  try {
    const p = await api('/api/presets');
    presets = p.presets || {};
    mimoVoices = p.mimo_voices || [];
    mimoReady = !!p.mimo_ready;
    mimoKeyMasked = p.mimo_key_masked || '';
    mimoModes = p.mimo_modes || [];
    mimoClones = p.mimo_clones || [];
    mimoDesignDefault = p.mimo_design_default || '';
    if (p.mimo_clone_max) mimoCloneMax = p.mimo_clone_max;
  } catch (e) { }
  await loadShelf();
})();
