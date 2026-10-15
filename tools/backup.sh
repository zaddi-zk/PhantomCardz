#!/bin/bash
cd /home/milo/PhantomCardz || exit 1
mkdir -p backups
STAMP=$(date +%Y%m%d-%H%M%S)
sqlite3 market.db ".backup backups/market-$STAMP.db"
gzip backups/market-$STAMP.db
ls -1t backups/market-*.db.gz | tail -n +15 | xargs -r rm --
echo "backup complete: backups/market-$STAMP.db.gz"