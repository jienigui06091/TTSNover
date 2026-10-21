'use strict';
/**
 * 离线端到端测试：本地起一个假书源站，跑完 search → info → toc → content。
 * 不依赖外网，`npm test` 直接跑。
 */
const http = require('http');
const { execFile } = require('child_process');
const assert = require('assert');
const path = require('path');

const CLI = path.join(__dirname, 'cli.js');
const PORT = 18099;
const BASE = `http://127.0.0.1:${PORT}`;

const PAGES = {
  '/search': `<html><body>
    <div class="item"><img src="/c.jpg">
      <h3 class="t"><a href="/book/1">测试书</a></h3>
      <div class="a">作者：老王</div><div class="k">玄幻</div>
      <div class="u">第3章 结束</div><div class="i">一句简介</div>
    </div>
    <div class="item"><img src="/c2.jpg">
      <h3 class="t"><a href="/book/2">第二本</a></h3>
      <div class="a">作者：小李</div><div class="k">仙侠</div>
      <div class="u">第1章 开始</div><div class="i">另一句简介</div>
    </div></body></html>`,
  '/book/1': `<html><body><h1>测试书</h1><div class="author">老王</div>
    <img id="cover" src="/c.jpg"><div id="intro">真正的简介</div></body></html>`,
  // 站点按「最新在前」排列，测试自动翻转
  '/toc': `<html><body><div id="list">
    <a href="/c/3">第3章 结束</a><a href="/c/2">第2章 中段</a><a href="/c/1">第1章 开始</a>
    </div></body></html>`,
  '/c/1': `<html><body><div id="content"><p>段落一。</p><p>段落二。</p></div></body></html>`,
  '/c/2': `<html><body><div id="content"><p>中段正文。</p></div></body></html>`,
  '/c/3': `<html><body><div id="content"><p>结尾正文。</p></div></body></html>`,
};

const SRC = {
  bookSourceName: '本地测试源',
  bookSourceUrl: BASE,
  searchUrl: '/search?q={{key}}',
  ruleSearch: {
    bookList: '.item', name: '.t@text', author: '.a@text##作者：', bookUrl: 'a.0@href',
    coverUrl: 'img@src', kind: '.k@text', lastChapter: '.u@text', intro: '.i@text',
  },
  ruleBookInfo: {
    name: 'h1@text', author: '.author@text', intro: '#intro@text',
    coverUrl: '#cover@src', tocUrl: '',
  },
  ruleToc: { chapterList: '#list a', chapterName: 'text', chapterUrl: 'href' },
  ruleContent: { content: '#content@html' },
};

function run(job) {
  return new Promise((resolve, reject) => {
    const p = execFile('node', [CLI], { timeout: 20000 }, (err, stdout) => {
      if (!stdout) return reject(err || new Error('无输出'));
      try { resolve(JSON.parse(stdout)); } catch (e) { reject(new Error('JSON 解析失败: ' + stdout.slice(0, 200))); }
    });
    p.stdin.end(JSON.stringify(job));
  });
}

const cases = [];
const test = (name, fn) => cases.push({ name, fn });

test('搜索：列表/名称/作者正则/相对链接转绝对', async () => {
  const r = await run({ cmd: 'search', key: '测试', sources: [SRC] });
  const books = r.results[0].books;
  assert.strictEqual(books.length, 2, '应搜到 2 本');
  assert.strictEqual(books[0].name, '测试书');
  assert.strictEqual(books[0].author, '老王', '##作者： 应被剥掉');
  assert.strictEqual(books[0].bookUrl, BASE + '/book/1', '相对链接应转绝对');
  assert.strictEqual(books[0].coverUrl, BASE + '/c.jpg');
  assert.strictEqual(books[1].name, '第二本');
});

test('详情：ruleBookInfo 各字段', async () => {
  const r = await run({ cmd: 'info', source: SRC, bookUrl: BASE + '/book/1' });
  assert.strictEqual(r.info.name, '测试书');
  assert.strictEqual(r.info.author, '老王');
  assert.strictEqual(r.info.intro, '真正的简介');
  assert.strictEqual(r.info.coverUrl, BASE + '/c.jpg');
});

test('目录：倒序自动翻转 + 链接绝对化', async () => {
  const r = await run({ cmd: 'toc', source: SRC, bookUrl: BASE + '/book/1', tocUrl: BASE + '/toc' });
  assert.strictEqual(r.chapters.length, 3);
  assert.deepStrictEqual(r.chapters.map((c) => c.name), ['第1章 开始', '第2章 中段', '第3章 结束']);
  assert.strictEqual(r.chapters[0].url, BASE + '/c/1');
});

test('正文：HTML 转段落文本', async () => {
  const r = await run({ cmd: 'content', source: SRC, chapterUrl: BASE + '/c/1', bookUrl: BASE + '/book/1' });
  assert.strictEqual(r.content, '段落一。\n段落二。');
});

test('测试命令：单源体检返回条数', async () => {
  const r = await run({ cmd: 'test', source: SRC, key: '测试' });
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.count, 2);
});

(async () => {
  const srv = http.createServer((req, res) => {
    const body = PAGES[req.url.split('?')[0]];
    if (!body) { res.writeHead(404); return res.end('nope'); }
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(body);
  });
  await new Promise((r) => srv.listen(PORT, '127.0.0.1', r));

  let pass = 0, fail = 0;
  for (const c of cases) {
    try {
      await c.fn();
      console.log('  ✓', c.name);
      pass++;
    } catch (e) {
      console.log('  ✗', c.name, '\n     ', e.message);
      fail++;
    }
  }
  srv.close();
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
