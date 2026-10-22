# 部署到云服务器

从零在一台干净的 Ubuntu 24.04（2 核 2 GB / 20 GB 盘起步）上把本服务跑起来。
下面的域名、IP、路径按自己的改，其余可直接复制。

整体形状：

```
浏览器 / App ──HTTPS 443──> Caddy ──HTTP 127.0.0.1:8848──> uvicorn(app:app)
                                                              ├─ SQLite  /var/lib/webreader/reader.db
                                                              ├─ 缓存    /var/lib/webreader/tts/<uid>/
                                                              └─ node 子进程 srce/cli.js（搜书）
```

8848 **不对外**，公网只开 80/443。

## 0. 前置

- 域名 A 记录指向这台机器（例：`reader.example.com`）
- 云厂商安全组放行 **80、443**；8848 不要放行
- 规格：2 核 2 GB 能跑（搜书并发已限 `SEARCH_CONC=10`、全服 2 个搜索任务，峰值约 700 MB）；
  宽裕用 2 核 4 GB。磁盘按书和朗读缓存增长，20 GB 起步
- 出网：`api.fish.audio`（Fish TTS）、`api.sgroup.qq.com` 无关、各书源站点需要能直连；
  云服务器一般直连即可，不用代理

## 1. 系统准备

```bash
# 运行用户（不能用 root 跑服务）
sudo useradd --system --create-home --home-dir /var/lib/webreader --shell /usr/sbin/nologin webreader

# 代码 + 数据目录
sudo mkdir -p /opt/webreader /var/lib/webreader
sudo chown webreader:webreader /var/lib/webreader
sudo chmod 750 /var/lib/webreader

# 依赖：Python 3.12、Node（搜书子进程用）、Caddy
sudo apt update
sudo apt install -y python3-venv python3-pip nodejs npm git curl rsync
# Ubuntu 24.04 自带 node 18.19，够 cheerio 用；想用 20 走 NodeSource
node -v
```

## 2. 拉代码 + 装依赖

```bash
sudo git clone https://github.com/jienigui06091/TTSNover.git /opt/webreader
cd /opt/webreader
sudo git checkout feat/multiuser      # 多用户版在这个分支，main 还是旧的

# Python 依赖
sudo python3 -m venv .venv
sudo .venv/bin/pip install -U pip
sudo .venv/bin/pip install -r requirements.txt

# 搜书用的 node 依赖（cheerio）
cd /opt/webreader/srce && sudo npm install --omit=dev

sudo chown -R webreader:webreader /opt/webreader
```

> 私有仓库才需要 token/Deploy Key；这个仓库是 public，直接 clone。

## 3. 搬数据（把本机的书、书源、设置、缓存带过去）

**本机**（老机器）先停服务再取快照，别直接 `rsync data/`——WAL 模式下正在跑的库会拷出旧状态：

```bash
systemctl --user stop webreader
cd /home/liumanu/code/webreader
python3 - <<'PY'
import sqlite3
src = sqlite3.connect("data/reader.db"); dst = sqlite3.connect("/tmp/reader-snapshot.db")
with dst: src.backup(dst)
dst.close(); src.close()
PY
```

然后推到云服务器：

```bash
# 库
scp /tmp/reader-snapshot.db root@SERVER:/var/lib/webreader/reader.db
# 缓存 + 克隆样本（可选，几十 MB 起；不带也能用，只是首听要重新合成）
rsync -az --info=progress2 data/tts/ root@SERVER:/var/lib/webreader/tts/
rsync -az data/mimo_clones/ root@SERVER:/var/lib/webreader/mimo_clones/
# 主密钥：丢了它，用户存的 API Key 全部解不开（也可改用 WEBREADER_MASTER_KEY 环境变量）
scp data/secret.key root@SERVER:/var/lib/webreader/secret.key
```

**云服务器**上收尾：

```bash
sudo chown -R webreader:webreader /var/lib/webreader
sudo chmod 700 /var/lib/webreader
sudo chmod 600 /var/lib/webreader/secret.key
ls -l /var/lib/webreader/          # reader.db / secret.key / tts/ / mimo_clones/
```

> 全新开站就不搬，第 7 步注册后自动建库（首次启动 `app.py` 会 `init_db`）。
> 老库的账号会一起搬过去，登录密码不变；想重新来过就在**本机**按
> 「重置为未认领」的流程处理后再搬。

## 4. systemd

```bash
sudo cp /opt/webreader/deploy/webreader.service /etc/systemd/system/webreader.service
sudo systemctl daemon-reload
sudo systemctl enable --now webreader
systemctl status webreader --no-pager
journalctl -u webreader -n 30 --no-pager
```

监听确认（应该只有 127.0.0.1:8848）：

```bash
ss -lntp | grep 8848
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8848/login   # 200
```

## 5. HTTPS（Caddy）

```bash
sudo apt install -y caddy        # 若第 1 步没装
sudo cp /opt/webreader/deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile   # 改域名和邮箱
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

证书自动签发，几秒后：

```bash
curl -sI https://reader.example.com/login | head -3      # HTTP/2 200
curl -s  https://reader.example.com/api/books            # {"error":"请先登录"}
```

登录后浏览器里 `Set-Cookie` 应带 `Secure; HttpOnly; SameSite=Lax`
（`--proxy-headers` + Caddy 的 `X-Forwarded-Proto` 一起生效才有 `Secure`）。

## 6. 防火墙

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable && sudo ufw status
```

云厂商安全组同步只放 22/80/443。

## 7. 开站验收

1. 打开 `https://reader.example.com/` → 跳 `/login`
2. 注册第一个账号：
   - 搬了老库 → 会自动认领老数据（书架书、书源、设置、小米 key、缓存都归这个账号）
   - 全新库 → 空书架，先加书源再搜书
3. 逐项点一遍：搜书 → 加书架 → 打开章节 → 三种翻页 → 朗读 → 设置里填自己的 Fish / 小米 Key
4. App 侧：`POST /api/auth/login` 拿 `token`，之后带 `Authorization: Bearer <token>`；接口文档 `/api/docs`

## 8. 备份与恢复

```bash
sudo install -m 755 /opt/webreader/deploy/backup.sh /usr/local/bin/webreader-backup
sudo WEBREADER_DB=/var/lib/webreader/reader.db DEST=/var/backups/webreader /usr/local/bin/webreader-backup
# 每天 3:20
echo '20 3 * * * root WEBREADER_DB=/var/lib/webreader/reader.db DEST=/var/backups/webreader /usr/local/bin/webreader-backup >/var/log/webreader-backup.log 2>&1' \
  | sudo tee /etc/cron.d/webreader-backup
```

- 备份脚本用 `sqlite3.backup()` 取一致快照，**不要**用 `cp reader.db`（WAL 会漏写）
- `secret.key` 单独离线存一份（密码管理器/U 盘），别和备份放同一块盘
- 恢复：停服 → 解压覆盖 `reader.db` → 启服

```bash
sudo systemctl stop webreader
gunzip -c /var/backups/webreader/reader-20261022-032000.db.gz | sudo tee /var/lib/webreader/reader.db >/dev/null
sudo chown webreader:webreader /var/lib/webreader/reader.db
sudo systemctl start webreader
```

## 9. 升级

```bash
cd /opt/webreader
sudo git pull --ff-only
sudo .venv/bin/pip install -r requirements.txt
sudo npm --prefix srce install --omit=dev
sudo systemctl restart webreader
```

改表结构的大版本（比如再动多用户）先在**副本**上跑一遍 `python3 tools/migrate_multiuser.py --db /tmp/copy.db`，
再对线上库操作；迁移脚本自己会先备份。

## 10. 公网要注意的

- **开放注册**：任何人都能注册用你的 CPU/带宽合成 TTS、跑搜书。想收口就把注册关掉或加邀请码
  （`app.py` 的 `/api/auth/register`），或者只在内网/给朋友用
- **API Key 归属**：每个用户必须填自己的 Fish/小米 Key，服务端不提供公共 Key，没有成本外泄
- **限流**：登录/注册同 IP 5 次/分（进程内计数，单机够用；多实例要换 Redis）
- **日志**：`journalctl -u webreader`；Caddy 日志在 `/var/log/caddy/`。不要把带 token 的请求头打进日志
- 别把 `data/` 或 `secret.key` 提交到 Git（`.gitignore` 已挡 `data/`）

## 11. 排障

| 现象 | 原因 / 处理 |
| --- | --- |
| 搜书全失败 | 搜书是 `node srce/cli.js` 子进程：`systemctl show webreader -p Environment` 里没 PATH 就给 unit 加 `Environment=PATH=/usr/local/bin:/usr/bin:/bin`，并确认 `srce/node_modules` 已装 |
| 登录后立刻又回登录页 | 反代没传 `X-Forwarded-Proto: https`（Cookie 带了 `Secure` 但页面是 http），检查 Caddy 配置和 uvicorn 的 `--proxy-headers` |
| 429 | 同 IP 5 次/分限流，等一分钟；`auth.rate_ok` 是进程内计数，重启清零 |
| TTS 报网络错误 | 云服务器出网被拦（安全组/NAT），`curl -sI https://api.fish.audio` 验证 |
| 502 | 后端没起来：`journalctl -u webreader -n 50`；常见是 `WEBREADER_DATA` 权限或 venv 路径不对 |
| 内存飙高 | 搜书每个源一个 node 子进程（实测峰值约 78 MB），`SEARCH_CONC=10` 最坏约 780 MB。2 GB 机器降到 `SEARCH_CONC=6`，或把 `SEARCH_BUDGET` 收紧到 8 秒。另外别频繁点「重试失效源」——失效源留在冷却里（连续失败 2 次自动跳过）时，一轮要跑的源数会明显更少 |
