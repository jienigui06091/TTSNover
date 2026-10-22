# 网页版阅读器（Legado 风格）

FastAPI + 原生 JS（无构建步骤、无前端框架）。**多用户**：开放注册，每人一套独立的书架、书源、进度、设置和 API Key。

- 服务：`http://0.0.0.0:8848/`（systemd 用户服务 `webreader.service`，开机自启）
- 首次访问会跳到 `/login`：**第一个注册的账号自动接管原有数据**（书架/书源/缓存/小米 key），之后注册的账号从空数据开始
- 局域网：手机同 WiFi 打开 `http://192.168.2.108:8848/`
- 数据：`data/reader.db`（SQLite，全部表带 `user_id`）+ `data/tts/<user_id>/`（每人独立音频缓存）+ `data/secret.key`（API Key 加密主密钥）

### 多用户与鉴权

| 项 | 做法 |
|---|---|
| 注册 | 开放注册，无管理员/普通用户之分；用户名 2–32 位，密码 ≥8 位 |
| 密码 | `hashlib.scrypt`（n=2^14, r=8, p=1）+ 每用户随机 salt |
| 会话 | 30 天；库里只存 `sha256(token)`，明文 token 只在客户端 |
| Web 登录 | `HttpOnly` + `SameSite=Lax` Cookie（HTTPS 下自动加 `Secure`） |
| App 登录 | `Authorization: Bearer <token>`，与 Cookie 共用同一张 `tokens` 表 |
| 隔离 | `books` / `settings(user_id,k)` / `sources(user_id,id)` / `user_keys` / TTS 缓存与克隆样本目录全部按用户 |
| API Key | 每人自己的 Fish / 小米 key，AES-GCM 加密存 `user_keys`；接口只回 `has` + 掩码（`sk-****3f9a`），明文永不回前端 |
| 限流 | 登录/注册 5 次/分钟/IP；TTS 120 次/分钟/用户；搜书全服同时 2 个任务，单任务最多 `SEARCH_CONC`（默认 10）个 node 子进程 |

环境变量（`systemd` 里已配）：`WEBREADER_DATA`（数据目录，默认 `./data`）、`WEBREADER_DB`（单独指定库文件）、`AUTH_ENFORCE=1`、`SEARCH_CONC`、`SEARCH_MAX`、`TTS_PER_MIN`、`WEBREADER_MASTER_KEY`（base64 32 字节，优先于 `data/secret.key`）。

### 从单用户库升级

```bash
cp data/reader.db data/reader.db.bak-$(date +%Y%m%d-%H%M%S)
python3 tools/migrate_multiuser.py --db data/reader.db   # 单事务，失败整体回滚
systemctl --user restart webreader
# 然后浏览器打开 8848，第一个注册的账号会认领老数据
```

## 已经能用的功能

| 功能 | 说明 |
|---|---|
| 书架 | 封面网格、章节数、阅读进度百分比；**删除**：卡片右上角垃圾桶单本移除，或点顶栏「多选」→ 勾选 / 「全选」→「删除选中」（书籍行和章节正文一起删，手机上垃圾桶常显） |
| 导入 | 上传 `.txt` / `.epub`；TXT 自动识别 UTF-8 / GBK / GB18030 / Big5 |
| 目录 | 抽屉式章节列表，当前章高亮，目录内全文搜索 |
| 阅读 | 三种翻页方式（滚动 / 整页 / 左右），**章节无缝衔接**（读完自动接下一章，不跳转、不回滚），点击某句从该句朗读，滑动与方向键翻页 |
| 排版 | 字号 14–30、行距 1.4–2.2、边距、4 种字体、5 套主题（白/米黄/护眼/夜间/纯黑） |
| 进度 | 按「章 + 滚动位置」自动保存，换设备/刷新都接着读 |
| 朗读 | 按语义单元合成（纯标点不送模型）、逐句高亮、预取下一段、**读完自动进下一章**（下一章首句提前合成，衔接无空档）、音频缓存 |
| TTS 引擎 | 浏览器自带语音（离线）/ 任意 HTTP 接口（Fish Audio、**小米 MiMo v2.5**、硅基流动、OpenAI 兼容、Azure SSML） |
| 音色 | **独立输入框**，不写死；改完点「测试发音」立刻验证 |
| 跨设备 | 排版与 TTS 设置存在服务端，手机和电脑共用一份 |
| **书源搜索** | 导入 Legado 书源 JSON，多源并发搜书名，一键加入书架直接读 |

## 书源（网络小说）

书架右上角 🔍 搜索，`源` 进书源管理。

1. **导入**：`源` → 右上「导入」→ 选 Legado 的 `shareBookSource.json`（数组或单个对象都行，重复导入按 URL+名称去重更新）
2. **添加单个/链接**：`源` 顶部「添加」→ 粘贴一段书源 JSON，或填一个 json/txt 直链（由服务端下载，避开浏览器跨域）。同样按 URL+名称去重更新
3. **删除**：每个源行尾「删除」→ 确认后从库里移除（要恢复得重新导入）；要批量清理点「多选」→ 勾选或「全选」（只选当前筛选结果）→「删除选中」
4. **搜索**：🔍 → 输入书名回车。91 个源并发抓，命中的源按书数排序，每本可看详情/试读
5. **加入书架**：详情页「加入书架」→ 自动存目录（几千章也只存标题+链接）→ 打开就是第一章，正文按需抓取并落库
6. **启停**：每个源一个开关，关掉的不参与搜索（搜索更快）；「测试」用当前搜索框里的词单测这个源
7. **体检**：`python3 tools/src_health.py` 用「剑来」「诡秘之主」测所有源，两个都搜不到就自动停用
8. **导出**：`源` 顶部「导出启用」/「导出全部」→ 下载 `书源-启用-YYYYMMDD.json`（Legado 分享格式，可直接导入「阅读」App）。导出会带上库里的启用状态：`enabled` 反映当前开关，停用的源 `enabled:false`，`bookSourceGroup` 用库里的分组。命令行等价：`curl -OJ 'http://127.0.0.1:8848/api/sources/export?only=1'`

规则引擎在 `srce/`（Node，唯一外部依赖是 `cheerio`）：

| 文件 | 作用 |
|---|---|
| `srce/engine.js` | Legado 规则解释器：jsoup 简化语法（`class.x` / `id.x` / `tag.N` / `!N` / `[a:b]`）、`@css:`、JSONPath（含省略 `$.` 写法）、`##正则##替换`、`&&` / `||` / `%%` 串联、`{{JS 表达式}}` 模板、`@js:` 与 `<js>` 脚本、`java.ajax` 等桩 |
| `srce/cli.js` | 命令分发：`search` / `info` / `toc` / `content` / `test`，stdin JSON 进、stdout JSON 出 |
| `srce/batch_test.js`、`diag.js` | 批量测源 / 抓包诊断 |

为什么用 Node：Legado 的 `{{}}` 模板和 `@js:` 规则本质就是 JS（Rhino），Python 里没有等价物；cheerio 的容错解析也比手写 HTML 解析器稳。Python 服务只做 `subprocess` 桥接，其余仍是标准库。

书源抓的是第三方站点正文，仅供自己看，不要二次分发。

## 朗读设置怎么填

书架右上角 ⚙ 或阅读页右上角 `Aa` → **朗读** 标签页。

1. **预设**：先选一家（Fish Audio / 小米 MiMo / 硅基流动 / OpenAI 兼容 / Azure / 浏览器语音），会自动填好 URL、请求头、请求体
2. **音色 ID**：Fish Audio 就是音色页地址栏 `/model/` 后面那串，自己填；小米不用管这一栏（它有三种音色来源，见下）
3. **测试发音**：直接告诉你结果——成功显示字节数/格式/耗时，失败显示 HTTP 状态码和接口原话
4. **保存**

### 小米 MiMo TTS（新增）

| 项 | 值 |
|---|---|
| 接口 | `POST https://api.xiaomimimo.com/v1/chat/completions`（OpenAI 兼容，国内直连，不用代理） |
| 模型 | 三种音色来源各一个模型：`mimo-v2.5-tts`（预置）、`mimo-v2.5-tts-voicedesign`（音色设计）、`mimo-v2.5-tts-voiceclone`（音色克隆） |
| 鉴权 | `Authorization: Bearer <key>`，key 只在服务端，前端拿不到 |
| Key 存放 | `data/mimo.key`（`chmod 600`，`data/` 已被 git 忽略），或环境变量 `MIMO_API_KEY`；也可在设置页粘贴后点保存写入 |
| 预置音色 | `mimo_default`、冰糖、茉莉、苏打、白桦、Mia、Chloe、Milo、Dean |
| 音色设计 | 设置页选「音色设计」，用一两句话描述（性别年龄/音色质感/口音/语速情绪）当 `user` 消息，模型按描述现造音色；不传 `audio.voice`，`assistant` 仍是正文。合成速度接近预置（实测 ~2–5 秒/句） |
| 音色克隆 | 设置页选「音色克隆」，上传 mp3/wav 样本（几秒到一分钟干净人声，≤7 MB），服务端存 `data/mimo_clones/` 并把样本转成 `data:audio/...;base64,...` 放进 `audio.voice`。**上游排队较飘（实测 3–170 秒/句）**，超时给到 240 秒，同句同样本第二次命中缓存秒回 |
| 返回 | JSON 里 `choices[0].message.audio.data` 是 base64，取 `audio.format=mp3` 解码后直接播（64 kbps / 24 kHz，比 wav 小约 7 倍） |
| 语速 | 接口没有语速参数 → 由前端 `playbackRate` 变速播放（`preservesPitch` 保持音调），**改语速不会重新合成**，缓存按「模型+音色/描述/样本+风格+正文」算，不含语速 |
| 风格指令 | 请求里的 `user` 消息是风格描述（如「自然、平稳的朗读语气」），`assistant` 消息才是正文；设置页「风格指令」可改（音色设计模式下会接在描述后面） |
| 克隆样本管理 | `POST /api/tts/mimo/clone`（name + filename + base64 data）上传，`POST /api/tts/mimo/clone/delete` 删除，`GET /api/presets` 里的 `mimo_clones` 返回列表（只给名字/大小） |

### 请求体模板的占位符

| 占位符 | 含义 |
|---|---|
| `{{text}}` | 正文，已做 JSON 字符串转义（不带外层引号） |
| `{{text_raw}}` | 正文原文，不转义 |
| `{{text_xml}}` | 正文 XML 转义（Azure SSML 用） |
| `{{voice}}` | 音色 ID，已做 JSON 转义 |
| `{{voice_raw}}` | 音色 ID 原文 |
| `{{voice_url}}` | 音色 ID，URL 编码（放在 URL 里用） |
| `{{speed}}` | 语速数字，如 `1.18` |
| `{{speed_pct}}` | 语速百分比变化，如 `18` |

### 为什么 TTS 走服务端

浏览器直连 `api.fish.audio` 会被 CORS 挡掉，而且本机需要 `127.0.0.1:7890` 代理才能出去。所以合成请求由服务端转发：key 不出现在前端代码里，代理可配，结果按 `文本+音色+语速` 哈希缓存到 `data/tts/`（同一句只花一次钱）。

## 朗读与翻页的几个细节

- **标点不送模型**：正文先按 `。！？；…` 断句，纯标点的句子（`……`、`——`、`「」`、`！？`）只留在页面上，不进朗读队列；相邻短句合并成约 90 字的朗读单元，超长句（>120 字）再按逗号切，避免一次请求太长被模型拒。
- **失败不中断**：某段合成失败（400 / 超时）跳过它继续读，连续失败 3 次才停，提示栏写明原因。
- **自动下一章**：本章最后一段播完，把下一章**拼到正文后面**接着读（同样不清屏、不跳转）；快读完时就把下一章正文拼好并预热它第一句，所以章节之间几乎听不出停顿。可在「朗读」设置里关掉。
- **章节无缝衔接（阅读）**：不再"读完一章清屏重载"，而是**把下一章直接拼到正文后面**——剩余可滚距离不足 2.5 屏时静默追加，滚动位置一点不动，所以你看到的是「· · ·」分隔点 + 下一章标题继续往下走，没有回滚、没有闪白。整页/左右模式同理（追加只延长内容，前面页的排版不受影响）。下一章正文提前取回内存，追加几乎瞬时。开关在 `Aa` →「读到底自动接下一章（无跳转）」，默认开；关掉后停在当前章末尾，用底部「下一章」按钮手动走。长读时自动丢弃头顶 3 屏以外的旧章节块（视觉无变化），DOM 不会无限膨胀。
- **翻页方式**：`Aa` → 翻页方式 三选一

| 模式 | 操作 |
|---|---|
| 滚动 | 常规上下滚；`←` `→` 直接上/下一章 |
| 整页 | 一次翻一屏；点右侧 62% 往后、左侧 38% 往前、中间开关朗读面板；上下滑也翻页；`←` `→` 翻页，翻到章末自动进下一章 |
| 左右 | 多列横向分页，一页一屏；左右滑或 `←` `→` 翻页；翻到章末自动进下一章 |

`PageUp` / `PageDown` 在任何模式下都是上/下一章，空格键开关朗读。

## 常用命令

```bash
systemctl --user status webreader      # 看状态
systemctl --user restart webreader     # 改完代码重启
journalctl --user -u webreader -f      # 看日志
```

端口改了记得同步 `~/.config/systemd/user/webreader.service` 里的 `ExecStart`。

## 目录结构

```
webreader/
├── app.py               # FastAPI 入口：路由 + 鉴权中间件 + 限流（唯一入口）
├── auth.py              # 账号/会话：scrypt 密码、tokens 表、双通道校验
├── keys.py              # 每用户 API Key 的 AES-GCM 加解密
├── claim.py             # 第一个账号认领历史数据
├── paths.py             # WEBREADER_DATA / WEBREADER_DB 路径解析
├── booklib.py           # TXT/EPUB 解析、章节切分、SQLite（全部按 user_id）
├── tts.py               # TTS 模板渲染 + 代理转发 + 按用户缓存 + 预设
├── sources.py           # 书源入库/启停 + 调 Node 引擎 + 加入书架 + 正文按需抓取
├── srce/                # Node 规则引擎（engine.js / cli.js，依赖 cheerio）
├── tools/migrate_multiuser.py  # 单用户库 → 多用户库迁移（备份 + 事务）
├── tools/src_health.py  # 书源体检，自动停用死源
├── static/{index.html,login.html,app.js,style.css}
├── requirements.txt     # fastapi / uvicorn / cryptography
├── sample/测试书.txt     # 演示用
└── data/                # 运行时生成（不进 Git）
```

> 旧的标准库版 `server.py` 已删除（多用户前无鉴权，留着容易被误启动），需要时从 Git 历史 `git show af1790e:server.py` 取。

## 云服务器 / App 部署

1. 装依赖：`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
2. 数据搬家：`WEBREADER_DATA=/var/lib/webreader`，把 `data/` 内容拷过去
3. **必须上 HTTPS**（否则密码/token 明文过网）：Caddy 两行搞定，Cookie 会自动带 `Secure`
   ```
   your.domain.com {
       reverse_proxy 127.0.0.1:8848
   }
   ```
4. systemd 用 `app.py` 的启动方式：`.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8848`（前面有 Caddy 时只监听 127.0.0.1）
5. App 端：`POST /api/auth/login` 拿 `token`，之后所有请求带 `Authorization: Bearer <token>`；接口文档 `GET /api/docs`
6. 容量参考：2 核 2 GB 能跑（搜书并发已限到 `SEARCH_CONC=10`，峰值约 700 MB）；宽裕用 2 核 4 GB + 20 GB 盘

## 还没做的（和原 App 的差距）

- 换源（同一本书在多个源之间切换）、整本缓存下载、离线包
- 需要登录/签名（`java.md5Encode` 之外的加密）的源暂时不支持
- 锁屏/后台朗读控制（网页受浏览器限制，熄屏会停）
- 自定义字体上传、阅读时长统计、笔记划线
