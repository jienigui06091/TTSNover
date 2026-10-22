#!/usr/bin/env bash
# 网页阅读器备份：用 sqlite backup API 取一致快照（WAL 库直接 cp 会漏写入）。
#
#   ./backup.sh                     # 备份到 /var/backups/webreader，保留 7 份
#   WEBREADER_DB=... DEST=... KEEP=3 ./backup.sh
#
# 注意：data/secret.key 是加密用户 API Key 的主密钥，**不在这里备份**。
# 丢了它，user_keys 里的 Key 全部解不开；请单独离线保存一份。
set -euo pipefail

DB="${WEBREADER_DB:-/var/lib/webreader/reader.db}"
DEST="${DEST:-/var/backups/webreader}"
KEEP="${KEEP:-7}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/reader-$STAMP.db"

[ -f "$DB" ] || { echo "找不到数据库：$DB" >&2; exit 1; }
mkdir -p "$DEST"

python3 - "$DB" "$OUT" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY

gzip -f "$OUT"
echo "已备份：$OUT.gz ($(du -h "$OUT.gz" | cut -f1))"

# 只留最近 KEEP 份
ls -1t "$DEST"/reader-*.db.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r f; do
	rm -f -- "$f"
	echo "清理旧备份：$f"
done
