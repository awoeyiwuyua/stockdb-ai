#!/bin/sh
set -e
cd /vol1/stockdb
echo xiahaihe | sudo -S sh -c '
cd /vol1/stockdb
sed -i "s/GIT_COMMIT: .*/GIT_COMMIT: \"48b6735\"/" compose.yaml
cp compose.yaml compose.yaml.bak-0.14.1
rm -rf src-new && mkdir src-new
tar xzf /tmp/src-0.14.1.tar.gz -C src-new
rsync -a --delete --exclude data --exclude mydb src-new/ src/
rm -rf src-new
sed -i "s/image: stockdb-ai:.*/image: stockdb-ai:0.14.1/" compose.yaml
cd /vol1/stockdb && docker compose build 2>&1 | tail -1
docker compose up -d 2>&1 | tail -1
sleep 20
docker ps --format "{{.Names}}|{{.Image}}|{{.Status}}" | head -1
docker exec stockdb sh -c "env | grep STOCKDB_BUILD"
head -1 /opt/webui/entrypoint.sh 2>/dev/null | od -c | head -1
docker exec stockdb sh -c "head -1 /opt/webui/entrypoint.sh | od -c | head -1"
'
