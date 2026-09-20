#!/bin/bash
# Cron бэкап SQLite — 02:00 ежедневно, хранит 7 дней
# crontab -e -> 0 2 * * * /opt/tg2gsheet/deploy/backup.sh
set -e
SRC="/opt/tg2gsheet/data/app.db"
DST="/opt/tg2gsheet/data/backup/app-$(date +%Y%m%d).db"
mkdir -p "$(dirname "$DST")"
if [ -f "$SRC" ]; then
  cp "$SRC" "$DST"
  chmod 600 "$DST"
  find "$(dirname "$DST")" -name "app-*.db" -mtime +7 -delete
  echo "[$(date)] backup $DST"
else
  echo "no db $SRC"
  exit 1
fi
