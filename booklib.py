# -*- coding: utf-8 -*-
"""书籍解析与存储：TXT / EPUB → SQLite（章节表）"""
import io
import json
import os
import re
import sqlite3
import time
import zipfile
from html.parser import HTMLParser

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.environ.get("WEBREADER_DB") or os.path.join(DATA, "reader.db")

# ---------- 章节识别 ----------
CN_NUM = "0-9零一二三四五六七八九十百千万两〇"
CH_PATTERNS = [
    re.compile(rf"^\s*第[{CN_NUM}]{{1,12}}[章节回卷集部篇幕][^\n]{{0,40}}$"),
    re.compile(r"^\s*(?:Chapter|CHAPTER)\s+\d{1,4}[^\n]{0,40}$"),
    re.compile(r"^\s*(?:序章|楔子|前言|引子|尾声|后记|终章|番外)[^\n]{0,20}$"),
    re.compile(r"^\s*\d{1,4}[、.．]\s*[^\n]{1,30}$"),
]
MAX_CHAPTER_CHARS = 12000


def decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 42:
        return False
    return any(p.match(s) for p in CH_PATTERNS)


def split_chapters(text: str):
    """返回 [(title, body), ...]"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    marks = [i for i, ln in enumerate(lines) if _is_heading(ln)]

    out = []
    if len(marks) >= 3:
        # 第一个标题之前的内容并入第一章
        head_end = marks[0]
        preface = "\n".join(lines[:head_end]).strip()
        for n, i in enumerate(marks):
            end = marks[n + 1] if n + 1 < len(marks) else len(lines)
            title = lines[i].strip()
            body = "\n".join(lines[i + 1:end]).strip()
            if n == 0 and preface:
                body = preface + "\n\n" + body
            out.append((title, body))
    else:
        # 兜底：按长度切
        paras = [p.strip() for p in text.split("\n") if p.strip()]
        buf, size, idx = [], 0, 1
        for p in paras:
            buf.append(p)
            size += len(p)
            if size >= 3500:
                out.append((f"第 {idx} 节", "\n\n".join(buf)))
                buf, size, idx = [], 0, idx + 1
        if buf:
            out.append((f"第 {idx} 节", "\n\n".join(buf)))

    # 超长章节再切，避免一次渲染太大
    final = []
    for title, body in out:
        if len(body) <= MAX_CHAPTER_CHARS:
            final.append((title, body))
            continue
        parts = [p for p in body.split("\n\n") if p.strip()]
        buf, size, k = [], 0, 1
        for p in parts:
            buf.append(p)
            size += len(p)
            if size >= MAX_CHAPTER_CHARS * 0.7:
                final.append((f"{title}（{k}）", "\n\n".join(buf)))
                buf, size, k = [], 0, k + 1
        if buf:
            final.append((title if k == 1 else f"{title}（{k}）", "\n\n".join(buf)))
    return final or [("正文", text.strip())]


# ---------- EPUB ----------
class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "h1", "h2", "h3", "h4", "li", "tr", "blockquote"}
    SKIP = {"script", "style", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self._skip, self.headings = [], 0, []
        self._in_head = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag in ("h1", "h2", "h3"):
            self._in_head = tag
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag in ("h1", "h2", "h3"):
            self._in_head = None
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        t = data.strip()
        if not t:
            return
        if self._in_head:
            self.headings.append(t)
        self.parts.append(t)

    def text(self):
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t\u3000]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _epub_opf(zf: zipfile.ZipFile):
    try:
        container = zf.read("META-INF/container.xml").decode("utf-8", "replace")
    except KeyError:
        raise ValueError("不是有效的 EPUB（缺少 container.xml）")
    m = re.search(r'full-path="([^"]+)"', container)
    if not m:
        raise ValueError("EPUB 缺少 OPF 路径")
    return m.group(1)


def parse_epub(raw: bytes):
    zf = zipfile.ZipFile(io.BytesIO(raw))
    opf_path = _epub_opf(zf)
    base = os.path.dirname(opf_path)
    opf = zf.read(opf_path).decode("utf-8", "replace")

    title = ""
    m = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", opf, re.S)
    if m:
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    author = ""
    m = re.search(r"<dc:creator[^>]*>(.*?)</dc:creator>", opf, re.S)
    if m:
        author = re.sub(r"<[^>]+>", "", m.group(1)).strip()

    manifest = {}
    for mm in re.finditer(r"<item\b[^>]*>", opf):
        tag = mm.group(0)
        idm = re.search(r'id="([^"]+)"', tag)
        href = re.search(r'href="([^"]+)"', tag)
        if idm and href:
            manifest[idm.group(1)] = href.group(1)

    spine = re.findall(r'<itemref[^>]*idref="([^"]+)"', opf)
    chapters = []
    for idref in spine:
        href = manifest.get(idref)
        if not href:
            continue
        path = os.path.normpath(os.path.join(base, href)).replace("\\", "/")
        try:
            html = zf.read(path).decode("utf-8", "replace")
        except KeyError:
            continue
        ex = _TextExtractor()
        ex.feed(html)
        body = ex.text()
        if len(body) < 20:
            continue
        name = ex.headings[0] if ex.headings else ""
        if not name:
            m2 = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
            name = re.sub(r"<[^>]+>", "", m2.group(1)).strip() if m2 else ""
        nm = name or f"第 {len(chapters) + 1} 节"
        # 正文开头常把标题又写一遍，去掉，避免和章标题重复
        lines = body.split("\n")
        while lines and (not lines[0].strip() or lines[0].strip() == nm.strip()):
            lines.pop(0)
        body = "\n".join(lines).strip()
        if not body:
            continue
        chapters.append((nm, body))

    if not chapters:
        raise ValueError("EPUB 里没解析出正文")
    return title or "未命名", author, chapters


# ---------- DB ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS books(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL, author TEXT DEFAULT '',
  fmt TEXT DEFAULT 'txt', chars INTEGER DEFAULT 0,
  created REAL, opened REAL, progress_ch REAL DEFAULT 0, progress_off REAL DEFAULT 0,
  seed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chapters(
  book_id INTEGER NOT NULL, idx INTEGER NOT NULL,
  title TEXT, content TEXT,
  PRIMARY KEY(book_id, idx)
);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS ix_ch ON chapters(book_id);
"""


def conn():
    os.makedirs(DATA, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)


def add_book(title, author, fmt, chapters):
    total = sum(len(b) for _, b in chapters)
    now = time.time()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO books(title,author,fmt,chars,created,opened,seed) VALUES(?,?,?,?,?,?,?)",
            (title, author or "", fmt, total, now, now, abs(hash(title)) % 997),
        )
        bid = cur.lastrowid
        c.executemany(
            "INSERT INTO chapters(book_id,idx,title,content) VALUES(?,?,?,?)",
            [(bid, i, t, b) for i, (t, b) in enumerate(chapters)],
        )
    return bid


def list_books():
    with conn() as c:
        rows = c.execute(
            """SELECT b.*, (SELECT COUNT(*) FROM chapters WHERE book_id=b.id) AS ch_count
               FROM books b ORDER BY opened DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_book(bid):
    with conn() as c:
        r = c.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["chapters"] = [
            {"idx": x["idx"], "title": x["title"]}
            for x in c.execute(
                "SELECT idx,title FROM chapters WHERE book_id=? ORDER BY idx", (bid,)
            )
        ]
    return d


def get_chapter(bid, idx):
    with conn() as c:
        r = c.execute(
            "SELECT title,content FROM chapters WHERE book_id=? AND idx=?", (bid, idx)
        ).fetchone()
        n = c.execute("SELECT COUNT(*) FROM chapters WHERE book_id=?", (bid,)).fetchone()[0]
        c.execute("UPDATE books SET opened=? WHERE id=?", (time.time(), bid))
    if not r:
        return None
    paras = [p.strip() for p in r["content"].split("\n") if p.strip()]
    return {"idx": idx, "title": r["title"], "paras": paras, "count": n}


def set_progress(bid, ch, off):
    with conn() as c:
        c.execute(
            "UPDATE books SET progress_ch=?,progress_off=?,opened=? WHERE id=?",
            (int(ch), float(off), time.time(), bid),
        )


def delete_book(bid):
    with conn() as c:
        c.execute("DELETE FROM chapters WHERE book_id=?", (bid,))
        c.execute("DELETE FROM books WHERE id=?", (bid,))


def search(bid, kw, limit=40):
    like = f"%{kw}%"
    out = []
    with conn() as c:
        for r in c.execute(
            "SELECT idx,title,content FROM chapters WHERE book_id=? AND content LIKE ? ORDER BY idx LIMIT ?",
            (bid, like, limit),
        ):
            txt = r["content"]
            p = txt.find(kw)
            s = max(0, p - 30)
            out.append(
                {"idx": r["idx"], "title": r["title"], "snippet": txt[s:s + len(kw) + 60]}
            )
    return out


def get_settings():
    with conn() as c:
        r = c.execute("SELECT v FROM settings WHERE k='main'").fetchone()
    return json.loads(r["v"]) if r else {}


def put_settings(obj):
    with conn() as c:
        c.execute(
            "INSERT INTO settings(k,v) VALUES('main',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (json.dumps(obj, ensure_ascii=False),),
        )
