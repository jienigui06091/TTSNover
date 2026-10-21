'use strict';
// 批量测源：node batch_test.js [关键词] [并发]
const fs = require('fs');
const E = require('./engine');

const all = JSON.parse(fs.readFileSync(process.env.SRC_JSON || '/home/liumanu/下载/shareBookSource.json', 'utf-8'));
const key = process.argv[2] || '剑来';
const limit = Number(process.argv[3] || 8);

async function run() {
  const out = [];
  let idx = 0;
  async function worker() {
    while (idx < all.length) {
      const src = all[idx++];
      const t0 = Date.now();
      let rec;
      try {
        const r = await E.doSearch(src, key, 1);
        if (r.error) rec = { name: src.bookSourceName, ok: false, err: r.error, ms: Date.now() - t0 };
        else rec = { name: src.bookSourceName, ok: r.books.length > 0, n: r.books.length, first: (r.books[0] || {}).name || '', ms: Date.now() - t0 };
      } catch (e) {
        rec = { name: src.bookSourceName, ok: false, err: String(e.message || e), ms: Date.now() - t0 };
      }
      out.push(rec);
      process.stderr.write(`${out.length}/${all.length} ${rec.ok ? '✓' : '✗'} ${rec.name}\n`);
    }
  }
  await Promise.all(Array.from({ length: limit }, worker));
  out.sort((a, b) => (b.ok - a.ok) || a.name.localeCompare(b.name));
  const okCount = out.filter((x) => x.ok).length;
  console.log(`\n可用 ${okCount} / ${all.length}\n`);
  for (const r of out) {
    console.log(`${r.ok ? '✓' : '✗'} ${r.name}${r.ok ? `  [${r.n} 条] ${r.first}` : '  ' + (r.err || '无结果')}  ${r.ms}ms`);
  }
}
run();
