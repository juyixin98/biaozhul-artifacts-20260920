#!/bin/sh
set -e

echo "等待数据库 ${DB_HOST}:${DB_PORT} ..."
until node -e "
const net=require('net');
const s=net.connect({host:process.env.DB_HOST,port:Number(process.env.DB_PORT)});
s.on('connect',()=>{s.end();process.exit(0)});
s.on('error',()=>process.exit(1));
" 2>/dev/null; do
  sleep 1
done
echo "数据库已就绪。"

echo "执行数据库迁移..."
node src/db/migrate.js

if [ "$AUTO_SEED" = "true" ]; then
  echo "播种演示数据..."
  node src/db/seed.js || true
fi

exec "$@"
