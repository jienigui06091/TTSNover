'use strict';
// 本地调试：node try.js "源名" 关键词
const fs = require('fs');
const E = require('./engine');

const all = JSON.parse(fs.readFileSync(process.env.SRC_JSON || '/home/liumanu/下载/shareBookSource.json', 'utf-8'));
const name = process.argv[2];
const key = process.argv[3] || '剑来';
const src = all.find((s) => s.bookSourceName.includes(name));
if (!src) { console.log('没找到源:', name); process.exit(1); }
console.log('源:', src.bookSourceName, src.bookSourceUrl);
const url = E.buildSearchUrl(src, key, 1);
console.log('搜索URL:', url);

E.doSearch(src, key, 1).then((r) => {
  if (r.error) { console.log('错误:', r.error); return; }
  console.log('结果数:', r.books.length);
  console.log(JSON.stringify(r.books.slice(0, 3), null, 1));
}).catch((e) => console.log('异常:', e.message));
