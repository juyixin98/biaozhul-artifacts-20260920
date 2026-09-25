# 请求样例（curl）

以下样例来自 `scripts/live-demo.sh` 的**真实运行**（2026-09-24，端口为临时
空闲端口；实际摘要以你的运行为准，下列摘要是本次运行记录）。先启动：

```bash
go build -o bin/bis ./cmd/bis
./bin/bis -addr 127.0.0.1:8080 -root ./.bis -timeout 15s
```

为简洁，下文中的 JSON 输出做了截断；完整响应见 `logs/live/*.json`。

## 1. 导入夹具工程

```bash
curl -sS -X PUT localhost:8080/projects/shared-demo \
  -H 'Content-Type: application/json' \
  -d "{\"src_dir\":\"$PWD/examples/shared-demo\"}"
```
```json
{"actions":3,"project":"shared-demo","src_dir":".../examples/shared-demo"}
```

## 2. 构建（3 个夹具命令真实执行）

```bash
curl -sS -X POST localhost:8080/projects/shared-demo/build
```
```json
{"status":"ok",
 "commands_run":[
   "tools/concat.sh out/a.bundle src/shared.txt src/a.txt",
   "tools/concat.sh out/b.bundle src/shared.txt src/b.txt",
   "tools/concat.sh out/final.txt in/a/out/a.bundle in/b/out/b.bundle"],
 "actions":[
   {"action_id":"compile_a","status":"executed",
    "outputs":[{"path":"out/a.bundle","bytes":103,
      "digest":{"hex":"ce581e6a10acf6261d5fac19553b181f46baba69a0ed44dcd358f9d8505c47fe"}}]},
   {"action_id":"compile_b","status":"executed","...":"..."},
   {"action_id":"link","status":"executed",
    "outputs":[{"path":"out/final.txt","bytes":252,
      "digest":{"hex":"e3fa140500138deb473a445a4e4e6639c8b7feb66aac46d8a4d507a15a64ce8a"}}]}]}
```

## 3. 再次构建（全部缓存命中，零命令执行）

```bash
curl -sS -X POST localhost:8080/projects/shared-demo/build
```
```json
{"status":"ok","actions":[
  {"action_id":"compile_a","status":"cached"},
  {"action_id":"compile_b","status":"cached"},
  {"action_id":"link","status":"cached"}]}
```

## 4. 验证证明链（完整）

```bash
curl -sS localhost:8080/projects/shared-demo/verify
```
```json
{"complete":true,"nodes":{...全部 "complete":true...},"findings":[]}
```

## 5. 源变更影响面：共享源

```bash
curl -sS "localhost:8080/projects/shared-demo/impact?path=src/shared.txt"
```
```json
{"matched_sources":["src/shared.txt"],
 "affected_actions":["compile_a","compile_b","link"],
 "affected_outputs":[
   {"action_id":"compile_a","output":"out/a.bundle","distance":1},
   {"action_id":"compile_b","output":"out/b.bundle","distance":1},
   {"action_id":"link","output":"out/final.txt","distance":2}],
 "chains":[
   {"source_path":"src/shared.txt","path":["compile_a","link"],"output":"out/final.txt"},
   {"source_path":"src/shared.txt","path":["compile_b","link"],"output":"out/final.txt"}]}
```

## 6. 源变更影响面：分支源（不跨分支污染）

```bash
curl -sS "localhost:8080/projects/shared-demo/impact?path=src/a.txt"
```
```json
{"matched_sources":["src/a.txt"],
 "affected_actions":["compile_a","link"]}
```
注意：`compile_b` 不读 `a.txt`，**不在**影响面内；`link` 经 `compile_a` 被传递影响。

按摘要查询（`path` 可省略）：

```bash
curl -sS "localhost:8080/projects/shared-demo/impact?digest=sha256:1594c3120a58..."
```

## 7. 查看 link 的签名溯源记录

```bash
curl -sS localhost:8080/projects/shared-demo/records/link
```
```json
{"record_id":{"hex":"113a2c784b8dc123f1e3fe5a13c64f726cc0038fe9e1d942cdd9d23d302c7735"},
 "record":{
   "action_id":"link",
   "tool":{"path":"tools/concat.sh","args":["out/final.txt","..."],
           "digest":{"hex":"ce297b67b941faee..."}},
   "sources":[],
   "upstreams":[
     {"action_id":"compile_a","output":"out/a.bundle","digest":{"hex":"ce581e6a10ac..."}},
     {"action_id":"compile_b","output":"out/b.bundle","digest":{"hex":"1e039d0a6c73..."}}],
   "outputs":[{"path":"out/final.txt","bytes":252,"digest":{"hex":"e3fa140500138deb..."}}],
   "input_fingerprint":{"hex":"5d9f953b43be45c6..."},
   "sig":"666a92c110857532..."}}
```

## 8. 隔离重算（可复现，200）

```bash
curl -sS -X POST localhost:8080/projects/shared-demo/reproduce
```
```json
{"reproduced":true,
 "commands_run":["...3 条命令，全部在全新空缓存中真实执行..."],
 "actions":[{"action_id":"link","status":"reproduced",
   "outputs":[{"output":"out/final.txt",
     "original_digest":"sha256:e3fa140500138deb...",
     "rerun_digest":"sha256:e3fa140500138deb...","reproduced":true}]}]}
```

## 9. 非确定性工程：溯源完整但不可复现（409）

```bash
curl -sS -X PUT  localhost:8080/projects/nondet-demo \
  -H 'Content-Type: application/json' -d "{\"src_dir\":\"$PWD/examples/nondet-demo\"}"
curl -sS -X POST localhost:8080/projects/nondet-demo/build
curl -sS -X POST localhost:8080/projects/nondet-demo/reproduce
```
```json
{"reproduced":false,
 "actions":[{"action_id":"make","status":"different",
   "outputs":[{"output":"out/x.txt","reproduced":false,
     "original_digest":"sha256:69950cebf0f3de43...",
     "rerun_digest":"sha256:2845934b350648ed..."}]}]}
```
同一工程 `GET .../verify` 仍返回 `complete:true` —— 链是真的，只是工具输出
不确定，直观展示两个性质相互独立。

## 10. 篡改后验证（409）

直接改导入副本里的源（或删一个缓存 blob）：

```bash
echo "tampered" >> .bis/projects/shared-demo/src/a.txt
curl -sS -i localhost:8080/projects/shared-demo/verify | head -1
```
```
HTTP/1.1 409 Conflict
```
```json
{"complete":false,
 "nodes":{
   "compile_a":{"complete":false,"tainted":true,
     "codes":["FINGERPRINT_MISMATCH","SOURCE_DIGEST_MISMATCH"]},
   "compile_b":{"complete":true},
   "link":{"complete":false,"tainted":true,"codes":["UPSTREAM_CHAIN_BROKEN"]}},
 "findings":[
   {"action_id":"compile_a","code":"SOURCE_DIGEST_MISMATCH",
    "message":"source \"src/a.txt\" changed: disk 6a5eff76.. != record 1594c312.."}]}
```

重新构建可恢复一致状态（新的源摘要产生新记录，`compile_a`、`link` 重新执行）。
