# 规则演进 / 回滚演练（curl）

以下示例展示“发布不可变规则 → 全量重建 → 回滚不改写证据”的完整过程。
先把 `BASE`、`WS`、`KEY` 换成你的环境值。

```bash
BASE=http://localhost:8000
WS=1
KEY=<seed-demo 返回的 api_key>
H="X-Workspace-Key: $KEY"

# 1) 查看现有版本（创建工作区时已内置 v1）
curl -s -H "$H" $BASE/api/workspaces/$WS/rules | python3 -m json.tool

# 2) 发布 v2：新增组织别名“北极星团队 -> 北极星”
curl -s -X POST -H "$H" -H 'Content-Type: application/json' \
  -d '{
    "note": "add 北极星 org",
    "rules": {
      "gazetteer": {
        "PERSON": [{"canonical": "赵六"}],
        "ORG":    [{"canonical": "北极星", "aliases": ["北极星团队"]}],
        "TECH":   [{"canonical": "Kafka"}]
      },
      "tech_patterns": [],
      "date_rules_enabled": true
    }
  }' $BASE/api/workspaces/$WS/rules/publish | python3 -m json.tool
# -> 202，返回 rebuild.job_id；重建期间检索仍读旧代

# 3) 等作业完成后，检索命中新代，实体按新规范名归一
curl -s -H "$H" "$BASE/api/workspaces/$WS/search?q=%E5%8C%97%E6%9E%81%E6%98%9F"
curl -s -H "$H" $BASE/api/workspaces/$WS/entities?type=ORG | python3 -m json.tool

# 4) v1 的历史证据仍可按 rule_version 读取，没有被改写
curl -s -H "$H" "$BASE/api/workspaces/$WS/entities?rule_version=1"

# 5) 回滚到完整旧代（若该规则没有现成完整代，会改为 202 触发重建）
curl -s -X POST -H "$H" -H 'Content-Type: application/json' \
  -d '{"version": 1}' $BASE/api/workspaces/$WS/rules/rollback | python3 -m json.tool

# 6) 同内容重复发布是幂等的：返回既有版本，不重建
curl -s -X POST -H "$H" -H 'Content-Type: application/json' \
  -d '{"rules": {"gazetteer": {"PERSON": [{"canonical": "赵六"}],
        "ORG": [{"canonical": "北极星", "aliases": ["北极星团队"]}],
        "TECH": [{"canonical": "Kafka"}]},
        "tech_patterns": [], "date_rules_enabled": true}}' \
  $BASE/api/workspaces/$WS/rules/publish | python3 -m json.tool
```
