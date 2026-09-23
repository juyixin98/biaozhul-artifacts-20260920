#!/usr/bin/env bash
# 初始化数据库账号与库（需要本机 sudo 权限访问 postgres 超级用户）。
set -euo pipefail

sudo -n -u postgres psql -v ON_ERROR_STOP=1 <<'SQL'
CREATE ROLE recon LOGIN PASSWORD 'recon';
CREATE DATABASE asset_recon OWNER recon;
CREATE DATABASE asset_recon_test OWNER recon;
SQL

echo "数据库已就绪：asset_recon / asset_recon_test（账号 recon，密码 recon）"
