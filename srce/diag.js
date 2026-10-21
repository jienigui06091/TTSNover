'use strict';
// 诊断：对每个源抓搜索页，判断「关键词是否送达」「bookList 是否命中」
const fs = require('fs');
const E = require('./engine');
const cheerio = require('cheerio');

const all = JSON.parse(fs.readFileSync(process.env.SRC_JSON || '/home/liumanu/下载/shareBookSource.json', 'utf-8'));
const key = process.argv[2] || '剑来';

(async () => {
  const rows = [];
  let idx = 0;
  async function worker() {
    while (idx < all.length) {
      const src = all[idx++];
      const ctx0 = { result: null, baseUrl: src.bookSourceUrl, key, page: 1, vars: {}, source: src };
      let rec = { name: src.bookSourceName, kind: '', n: 0 };
      try {
        const r = await E.fetchRule(src.searchUrl, ctx0, src);
        rec.code = r.code;
        rec.err = r.error || '';
        rec.len = (r.body || '').length;
        const body = r.body || '';
        rec.hasKey = body.includes(key) || body.includes(encodeURIComponent(key));
        let doc = null;
        if (/^\s*[\[{]/.test(body)) {
          try { JSON.parse(body); rec.kind = 'json'; } catch { rec.kind = 'html'; }
        } else rec.kind = 'html';
        if (rec.kind === 'html' && body) {
          doc = cheerio.load(body);
          const bl = (src.ruleSearch.bookList || '').trim();
          if (bl) {
            const { base } = (function () {
              const m = bl.match(/^(.*?)[.!:]-?\d+$/);
              return { base: m ? m[1] : bl };
            })();
            try { rec.sel = doc(base).length; } catch { rec.sel = -1; }
          }
        }
      } catch (e) {
        rec.err = String(e.message || e);
      }
      rows.push(rec);
      process.stderr.write(`${rows.length}/${all.length} ${rec.name}\n`);
    }
  }
  await Promise.all(Array.from({ length: 10 }, worker));
  rows.sort((a, b) => (b.sel || 0) - (a.sel || 0));
  for (const r of rows) {
    console.log([r.name, r.kind, 'code=' + r.code, 'len=' + r.len, 'key=' + (r.hasKey ? 'Y' : 'N'), 'sel=' + r.sel, r.err].join('\t'));
  }
})();
