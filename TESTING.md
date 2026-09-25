# 测试记录（TESTING）

本文件如实记录在开发机上**实际执行**过的验证命令与结果。环境：

- OS：Linux 6.8.0-90-generic (amd64)
- Go：go1.22.2 linux/amd64
- 工具：curl、jq、sha256sum、bash、python3（仅用于挑选空闲端口）
- 执行日期：2026-09-24

无云平台连接；全部走本地文件系统与 loopback HTTP。

## 1. Go 自动化测试

命令（实际执行，均通过）：

```bash
go build ./...
go vet ./...
gofmt -l .          # 无输出 = 全部已格式化
go test ./...
go test -race ./...
```

最近一次 `go test -race ./...` 结果：

```
ok  	modelcache/buildsvc	2.526s
ok  	modelcache/cacheapi	1.057s
ok  	modelcache/client	7.098s
ok  	modelcache/digest	1.016s
ok  	modelcache/store	1.047s
?   	modelcache/cmd/{buildctl,buildserver,cachectl,cacheserver}	[no test files]
```

测试覆盖（Go 层面）：

- `digest`：规范格式解析、往返、拒绝坏算法/坏 hex。
- `store`：
  - 上传/下载往返、发布对象权限为 0444；
  - 幂等重复上传；
  - **32 路同摘要并发上传 → 恰好一个对象、31 次 fast-path 命中、tmp 清空**；
  - 摘要不符 → `ErrDigestMismatch`、不发布、进入 quarantine；
  - 大小上限：恰好等于上限允许，超 1 字节拒绝且不留 tmp；
  - 声明长度不符拒绝；
  - 启动 Sweep 清除崩溃残留 tmp 且不碰 blobs；
  - 直接篡改已发布文件 → Verify 返回 `ErrCorrupt` 并隔离。
- `cacheapi`：上传/HEAD/GET/Range、坏摘要 400 且随后 404、24 路同摘要并发上传
  只有一个对象、损坏对象 `/verify` 返回 500 `corrupt` 且 `quarantined=true`、
  用同一目录重建 Store 模拟**重启持久化 + tmp 清扫**。
- `client`：
  - 验证后上传/下载；本地先算摘要，不符直接拒绝；
  - **截断下载**（首次全量响应只发一半）→ 保留 part、Range 续传、校验、
    原子 rename，part 文件消失；
  - 连续两次中断（全量+一次 Range）后续传成功；
  - 服务端忽略 Range（回 200）时整包重下；
  - 服务端返回等长错误内容 → `ErrDigestMismatch`，目标文件绝不发布；
  - 流式 Get 的哈希校验；8 个文件并发上传 + 每文件 3 路并发下载校验；
  - 404 正确映射为 `HTTPError`。
- `buildsvc`：只加载真实 `fixtures/manifest.json`；`${FIXTURES_DIR}` 被展开为
  绝对路径；成功夹具产物入缓存且在独立 artifact 目录留副本；失败夹具
  `status=failed, exitCode=7` 且缓存零增长；命令注入名、裸 `bash`、路径穿越
  夹具名全部拒绝；非法清单（带空格 command、`..`/绝对输出、坏名字）拒绝；
  slow-model 产物等于文档化 64 字节模式重复 1 MiB，摘要确定。

## 2. 端到端验收脚本

命令：

```bash
./scripts/acceptance.sh
```

最近一次最终结果（日志保存在开发机 `/tmp/acceptance-run6.log`，
运行产物在 `/tmp/modelcache-acceptance-ltKFBK/`）：

```
acceptance summary: 45 passed, 0 failed
```

脚本自动选择空闲 loopback 端口（也可用 `CACHE_PORT`/`BUILD_PORT` 覆盖），
实际构建并运行四个真实二进制（`cacheserver`/`buildserver`/`cachectl`/
`buildctl`），覆盖 12 组场景：

1. 夹具白名单：广告 4 个夹具；`"rm -rf /; echo pwned"` 与裸 `"bash"`
   作为夹具名均返回 HTTP 400 `unknown_fixture`。
2. 成功构建：`make-model` → `succeeded`，产物 4096 字节、sha256 摘要；
   缓存树中存在对象，work 树中不存在 blobs（**目录分离**）。
3. 客户端下载校验：`cachectl get` 成功且落盘 sha256 一致；错误摘要返回 404、
   不产生目标文件。
4. 坏内容上传：HTTP 400 `digest_mismatch`，随后 GET 404（**不暴露半成品**），
   `quarantine/` 中保留坏字节。
5. **同摘要 20 路并发 PUT**：全部 200，缓存中恰好 2 个不同对象，tmp 为 0。
6. 16 路上传期间 40 次并发 GET 探测：每个 200 响应均为完整且摘要正确的对象。
7. **截断下载/续传**：Range 精确返回 1 MiB，`Content-Range: bytes
   0-1048575/4194304`；预置 3 MiB 的 `.part` 后 `cachectl get` 用 Range 续传、
   校验、原子发布，`.part` 消失。
8. 大小上限（8 MiB）：12 MiB 上传返回 413 `object_too_large` 且不发布。
9. 失败夹具：`failed/exitCode=7`、零产物、缓存对象数不增长。
10. **坏缓存可诊断**：就地篡改分片路径下的对象后，`POST /verify` 返回
    HTTP 500 `code=corrupt, extra.quarantined=true`；之后 GET 404（自愈）；
    `quarantine/corrupt-*` 保留证据；`cachectl doctor` 对健康对象通过。
11. **服务重启**：植入 `tmp/upload-orphan-test.part`，杀掉缓存进程并以同一
    缓存目录重启；日志输出 `swept 1 orphaned temp upload(s)`，孤儿文件消失，
    两个对象均可下载且摘要一致。
12. `/v1/stats` JSON：`objects=2, tempFiles=0, maxObjectSize=8388608`。

## 3. 独立手动冒烟

除脚本外还手动构建四个二进制到临时目录、启动两个服务，经 `buildctl` 运行
`make-model`，再用 `cachectl list/stats/get/verify` 全链路验证：

- 作业产物（work 树）与 `cachectl get` 下载逐字节一致（`cmp`）；
- `stats` 显示 `temp files: 0`；
- 缓存树与工作树路径不同（separation）。

## 4. 开发过程中发现并修复的真实问题（如实记录）

- 初版大小限制读取器在“恰好读满上限”时把合法 EOF 误判为超限 → 重写为
  允许多读 1 字节来区分“恰满”与“超限”，并用边界测试锁定。
- 同摘要并发上传最初各自暂存、依赖 rename 覆盖；验收要求“恰好一次创建”，
  已改为按摘要 singleflight（一个领导者暂存，其余等待后命中已发布对象）。
- 断点续传在“整包到达但摘要不符”重试时未清空 part，会发出越界 Range
  导致 416；已改为不符即删除 part 整包重取，并把对续传的 416 也视为
  “丢弃 part、从头重下”。
- 4MiB 夹具初版用 bash 数万次小 `printf` 生成，60s 超时跑不完；改为
  awk 流式生成 + `head -c` 精确截断（注意 mawk 在 UTF-8 locale 下
  `length()` 按字符，脚本内强制 `LC_ALL=C`）。
- 验收脚本自身缺陷（非产品缺陷），均已修复：`curl -I/HEAD` 不返回
  Range 头；awk 取 `Content-Range` 列号错误；篡改对象路径漏了分片目录；
  两处无参数 `wait` 误等了仍在运行的服务进程导致挂起。

## 5. 未覆盖 / 已知边界（如实记录）

- 未实现鉴权与 TLS（面向本机/受控网络；需要时置于反向代理之后）。
- 未做磁盘配额驱动的 LRU 淘汰（提供手动 DELETE 与大小上限，未做自动驱逐）。
- 上传暂存期间服务被 `SIGKILL` 且恰好发生在 rename 之后、目录 fsync 之前的
  极端断电窗口依赖文件系统语义；常规重启路径（SIGTERM/SIGINT + 重启 Sweep）
  已验证。
- 未进行前端开发（按需求明确不做）。
