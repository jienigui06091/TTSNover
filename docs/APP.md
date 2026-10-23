# 写 App 时怎么接这个后端

后端已经按「Web 用 Cookie、App 用 Bearer」双通道做好，**写 App 不需要改服务端**。
这份文档是接口约定 + 客户端要处理的坑，配合 `/api/docs`（Swagger，线上免登录可看）一起用。

> **两条路线，别混着看**
>
> | 版本 | 形态 | 需要后端 | 数据在哪 |
> | --- | --- | --- | --- |
> | **1.1.0（本机模式，推荐给单机自用）** | `static/download/TTSNover-1.1.0.apk` | **不需要** | 全在手机 SQLite，只配书源 + TTS |
> | 1.0.0（服务器版） | `static/download/TTSNover-1.0.0.apk` | 需要（就是本仓库） | 服务端，多端同步 |
>
> 1.1.0 把 Node 版书源引擎（`srce/engine.js`）等价移植成了 Java（jsoup + Rhino），
> 抓书、解析规则、TTS 合成全在手机上完成，**下面的接口表对 1.1.0 全部不适用**。
> 两份实现的行为一致性用同一批 91 个书源做过对照：搜索命中数逐个源比对，差异 0。

## 1. 基址

- 线上：`https://<你的域名>`（Caddy 443 → `127.0.0.1:8848`），**App 里不要出现 8848**，也不要硬编码内网 IP，做成设置项。
- 本地调试：`http://127.0.0.1:8848`（Android 模拟器里宿主机是 `10.0.2.2`）。
- 全部走 HTTPS：Android 9+ 默认禁明文（`cleartextTrafficPermitted=false`），裸 IP + HTTP 会直接连不上。

## 2. 登录 / 注册 / 会话

| 动作 | 请求 | 返回 |
| --- | --- | --- |
| 注册 | `POST /api/auth/register` `{"username","password","device"}` | `{ok,user,token,expires_in:2592000}` |
| 登录 | `POST /api/auth/login` `{"username","password","device"}` | 同上 |
| 我是谁 | `GET /api/auth/me` | `{user:{id,username}, expires_at}` |
| 续期 | `POST /api/auth/refresh`（带旧 token） | `{ok,token,expires_in}` |
| 设备列表 | `GET /api/auth/sessions` | `{sessions:[{sid,device,ip,created,expires,last_used,current}]}` |
| 踢某个设备 | `DELETE /api/auth/sessions/{sid}` | `{ok,revoked}`；`sid` 太短/不是自己的 → 404 |
| 踢其它所有设备 | `POST /api/auth/logout_all` | `{ok,revoked}`（当前会话保留） |
| 退出 | `POST /api/auth/logout` | 清当前会话 + 清 Cookie |

要点：

1. **`device` 字段**建议传 `"Pixel 8 / TTSNover 1.1.0"` 这种，用户在「设备管理」里才认得出是哪台；不传则退回 `User-Agent`。
2. token 是 32 字节随机串，库里只存 `sha256`，**服务端也找不回明文**，丢了只能重新登录。
3. token 存 `EncryptedSharedPreferences`（或 Keystore 包装），别放普通 SharedPreferences / 日志。
4. 每个请求带 `Authorization: Bearer <token>`；`401 {"error":"请先登录"}` → 清 token 跳登录页。
5. 有效期 30 天，**没有单独的 refresh_token**：`POST /api/auth/refresh` 是「拿旧 token 换新 token」，
   旧 token 立刻失效（防止被复制后长期可用）。所以客户端要**先把新 token 落盘再丢弃旧的**，
   否则换发过程中被杀进程会掉登录态。

建议时机：冷启动时若 `expires_at` 距现在 < 7 天就 refresh 一次；或每次启动都 refresh（30 次/分钟限流，够用）。

## 3. 版本检查（强制升级）

`GET /api/version`（**免登录**，还没登录时也能查）：

```json
{"name":"TTSNover","version":"1.1.0","min_version":"1.0.0","api":1,"download_url":"","notes":""}
```

- `version` < 服务端 `version` → 提示可升级（非阻断）
- `version` < `min_version` → 阻断，只给「去下载」
- `api` 是接口大版本，跟 `min_version` 一起用，避免老客户端打到不兼容的接口

服务端用环境变量配（不用改代码）：`APP_VERSION`、`APP_MIN_VERSION`、`APP_DOWNLOAD_URL`、`APP_NOTES`。

## 4. 书架 / 目录 / 正文

| 用途 | 请求 | 备注 |
| --- | --- | --- |
| 书架 | `GET /api/books` | 返回 `{books,total}`，按 `opened` 倒序 |
| 分页 | `GET /api/books?limit=20&offset=0` | `limit=0`（默认）= 全部；`total` 始终是全量数 |
| 书详情 + 目录 | `GET /api/books/{bid}` | 含章节标题列表 |
| 正文 | `GET /api/books/{bid}/ch/{idx}` | `{idx,title,paras:[...],count}`，**分页由客户端算** |
| 记进度 | `POST /api/books/{bid}/progress` `{"ch":12,"off":0.37}` | 退出/切章时上报，别每翻一页都发 |
| 全书搜索 | `GET /api/books/{bid}/search?q=` | `{hits:[...]}` |
| 导入 TXT/EPUB | `POST /api/import?filename=书名.txt`，body 是文件原始字节 | 上限 60 MB |
| 删书 | `DELETE /api/books/{bid}` / `POST /api/books/delete {"ids":[...]}` | 单本 0 行 → 404；批量回真实 `deleted` |

书源书（`fmt=src`）的章节是**按需抓取**：第一次读 `ch/{idx}` 会现场请求上游，慢且可能失败，
客户端要显示「抓取失败，重试」而不是空页。

## 5. 搜书 / 书源

| 用途 | 请求 | 备注 |
| --- | --- | --- |
| 多源搜书 | `GET /api/src/search?q=&page=&include_dead=0&refresh=0` | 全服 2 个搜索任务、每用户 1 个；**429 要退避重试**；返回 `{key, results:[{source,sid,error,books[]}], stats:{sources,ok,books,skipped,cached,ms}}`；单次最多约 12 秒（服务端预算），做 loading + 取消 |
| 流式搜书 | `GET /api/src/search/stream?q=&page=` | `text/event-stream`，每行 `data: {...}`，`type` 依次为 `meta` → 多个 `source` → `done`（出错为 `error`）。浏览器 `EventSource` 不能带 `Authorization` 头，App 用上面的普通接口即可 |
| 失效源健康度 | `GET /api/sources/health` | 每源 `ok/fail/streak/skip_until`，`cooling=true` 表示正在冷却、不参与搜索 |
| 恢复失效源 | `POST /api/sources/health/reset` | 清掉全部冷却，下次搜索重新尝试 |
| 加入书架 | `POST /api/src/add` | 用搜索结果里的源 + 书 id |
| 书源列表 | `GET /api/sources` | 每用户独立 |
| 导入/导出 | `POST /api/sources/import` / `GET /api/sources/export?only=0` | 直接吃网页版导出的 JSON |

搜索结果里每本书带 `matched` 字段：`name`（书名命中）、`author`（作者命中）、空串（站点返回的模糊结果）。搜作者时建议只展示 `matched=author` 的书，或把它排在前面。

## 6. 朗读（TTS）

| 用途 | 请求 | 备注 |
| --- | --- | --- |
| 合成 | `POST /api/tts` `{"text","voice",...}` | 返回 `audio/mpeg` 字节流，带 `X-Cache: hit/miss` |
| 连通性自检 | `GET /api/tts/diag?provider=mimo` | 服务器 → 上游的 DNS/逐 IP TCP/HTTP 探测，`连不上上游` 时先看这里 |
| 预设/音色 | `GET /api/presets` | 含 Fish 音色、小米预置/设计/克隆 |
| 填 key | `POST /api/keys` `{"provider":"fish","key":"..."}` | 每用户自己的 key，AES-GCM 存库 |
| 看 key | `GET /api/keys` | **只回 `has_key` + 掩码**，拿不到明文 |

客户端要点：

0. 报 `连不上上游 / 连不上小米接口` 时，先打 `GET /api/tts/diag?provider=xxx`：返回 `ips`（DNS 解析结果）、
   `tcp`（每个 IP 的 connect 毫秒数，`ok:false` 就是那条路不通）、`http`（上游状态码，401/405 也算通）、
   `error`（人话原因）。**手机端不会直连厂商，这类错一定是服务器出口问题**，不要去查手机网络。

1. 一次请求可能 5–170 秒（上游慢），**读超时设 ≥300 秒**（Caddy 也是 300s）。
2. 命中缓存是毫秒级返回，`X-Cache: hit` 可以拿来做「秒播」判断。
3. 限流 `TTS_PER_MIN`（默认 120/分，按用户），超了 429。
4. 播放器用 Media3/ExoPlayer 直接吃字节流，**预取下一段**（朗读单元约 90 字）避免句间空档。
5. **后台朗读必须自己写**：`ForegroundService` + `MediaSession` + 通知栏（播放/暂停/上一章/下一章），
   `AndroidManifest` 声明 `FOREGROUND_SERVICE`、`FOREGROUND_SERVICE_MEDIA_PLAYBACK`、`POST_NOTIFICATIONS`（13+ 运行时申请）。
   不写这些，熄屏几分钟后系统就把进程回收了——网页版现在就是这个毛病。
6. 手机直连 `api.fish.audio` 常常 `Network is unreachable`，所以**必须走服务端 `/api/tts`**，不要客户端直连厂商。

## 7. 错误码约定

| 状态 | 含义 | 客户端动作 |
| --- | --- | --- |
| 400 | 参数不对（如 `ids` 空） | 提示，别重试 |
| 401 | 未登录 / token 过期或被踢 | 清 token → 登录页 |
| 404 | 书/章节/会话不存在（含越权，故意不区分） | 提示 + 刷新列表 |
| 413 | 导入文件超过 60 MB | 提示换文件 |
| 429 | 限流（登录 5/分/IP、搜书、TTS） | 指数退避，展示「稍后再试」 |
| 5xx | 服务端/上游异常（TTS 上游 402 余额不足等） | 提示原文，可重试 |

## 8. 还没做的（App 前可能要补）

- **CORS**：现在没配 `CORSMiddleware`。原生 App 不需要；用 WebView/H5 套壳才要加，并限定来源。
- **离线下载**：没有「整本缓存」接口，章节文本只能客户端自己存 Room。
- **音频缓存上限**：服务端 `data/tts/<uid>/` 无配额，长期用要考虑清理。
- **推送**：没有 FCM/厂商推送通道，更新提醒只能靠启动时查 `/api/version`。

## 9. 自检

改完后端跑一遍（会拷真库到临时目录，不碰线上数据）：

```bash
.venv/bin/python tools/verify_multiuser.py     # 期望 71 通过 / 0 失败
```

其中 H 组就是这份文档对应的能力：`/api/version`、书架分页、`device` 字段、设备列表、踢设备、
refresh 换发（旧 token 立刻失效）、`logout_all`。
