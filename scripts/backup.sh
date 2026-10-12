#!/usr/bin/env sh
# Sauvegarde quotidienne (base SQLite cohérente + factures), garde 30 jours.
# Crontab du VPS :  15 3 * * * /opt/wazzzxpense/scripts/backup.sh >> /var/log/wzx-backup.log 2>&1
set -eu
cd "$(dirname "$0")/.."
DEST=${BACKUP_DIR:-./backups}
mkdir -p "$DEST"
STAMP=$(date +%Y-%m-%d)
docker compose exec -T app python -c "
import sqlite3; s = sqlite3.connect('/data/wazzzxpense.db'); d = sqlite3.connect('/data/backup.db')
s.backup(d); d.close(); s.close()"
tar -czf "$DEST/wazzzxpense-$STAMP.tar.gz" -C data backup.db uploads
rm -f data/backup.db
find "$DEST" -name 'wazzzxpense-*.tar.gz' -mtime +30 -delete
echo "Sauvegarde OK : $DEST/wazzzxpense-$STAMP.tar.gz"
