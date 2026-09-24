# 围栏令牌资源保护（Fencing Token）本地双组件模拟

纯后端 Go 实现（仅标准库 `net/http`，无第三方依赖），在本地模拟两个组件：

- **锁服务**（`locksvc`）：基于租约（lease）的锁。每次把锁授予新持有者时发出**严格递增的围栏令牌**；令牌计数与租约落盘，进程重启后**计数不回退**。
- **资源服务**（`resourcesvc`）：受保护的键值存储。每个资源记录"已见最大令牌"，写请求携带的令牌必须 **≥ 已见令牌**，否则以 `409` 拒绝（等号允许同一持有者幂等重试）。

两个组件是独立 HTTP 服务（不同端口），通过**注入时钟**判断租约过期：生产用真实时钟（`clock.Real`），测试用手动时钟（`clock.Fake`）确定性推进时间。

## 依赖与启动

- 依赖：**Go ≥ 1.22**（用到 `net/http` 的方法路由模式，开发验证版本为 go1.23.4）；无第三方模块，`go.mod` 即锁定全部依赖（仅标准库）。
- 构建并启动（两个服务、一个进程、两个端口）：

```bash
go build -o fencing-server ./cmd/server
./fencing-server -lock-addr 127.0.0.1:18080 -res-addr 127.0.0.1:18081 -state-dir ./data
```

- 运行测试：

```bash
go test ./...        # 单元测试 + 集成验收测试
go test -race ./...  # 带竞态检测
```

- 端到端演示（需先启动服务器，用真实时钟等待租约过期，约 4 秒）：

```bash
bash scripts/demo.sh
```

## HTTP 接口

### 锁服务（默认 `127.0.0.1:18080`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/locks/{name}/acquire` | 获取/续租锁。Body：`{"holder":"A","ttl_ms":5000}` |
| POST | `/v1/locks/{name}/release` | 释放锁。Body：`{"holder":"A"}` |
| GET  | `/v1/locks/{name}` | 查询当前有效租约 |
| GET  | `/v1/counter` | 已发出的最大围栏令牌 |

### 资源服务（默认 `127.0.0.1:18081`）

| 方法 | 路径 | 说明 |
|---|---|---|
| PUT | `/v1/resources/{key}` | 写入。Body：`{"value":"...","token":2}` |
| GET | `/v1/resources/{key}` | 读取 |

## 请求样例（验收场景）

```bash
# 1) 旧持有者 A 获取锁 → 令牌 1
curl -X POST localhost:18080/v1/locks/config/acquire \
  -d '{"holder":"holder-A","ttl_ms":3000}'
# {"name":"config","holder":"holder-A","token":1,"expires_at":"..."}

# 2) 租约有效时 B 抢锁 → 409
curl -X POST localhost:18080/v1/locks/config/acquire \
  -d '{"holder":"holder-B","ttl_ms":3000}'
# HTTP 409 {"error":"lock held by another holder","holder":"holder-A",...}

# 3) 暂停 A（不续租），等待租约过期
sleep 4

# 4) B 获取锁 → 令牌 2（递增）
curl -X POST localhost:18080/v1/locks/config/acquire \
  -d '{"holder":"holder-B","ttl_ms":6000}'
# {"token":2,...}

# 5) 新持有者 B 写入 → 200
curl -X PUT localhost:18081/v1/resources/config \
  -d '{"token":2,"value":"value-from-B"}'

# 6) A 恢复，用旧令牌 1 迟到写入 → 409 被拒绝
curl -X PUT localhost:18081/v1/resources/config \
  -d '{"token":1,"value":"value-from-A-LATE"}'
# HTTP 409 {"error":"stale fencing token: write rejected","seen":2}

# 7) 重启服务器（同一 -state-dir），令牌不回退
curl localhost:18080/v1/counter          # {"counter":2}
curl -X PUT localhost:18081/v1/resources/config -d '{"token":1,"value":"x"}'   # 仍 409
curl -X POST localhost:18080/v1/locks/config/acquire -d '{"holder":"C","ttl_ms":5000}'
# {"token":3,...}  ← 在重启后的计数上继续递增
```

## 持久化与重启语义

`-state-dir` 下有两个 JSON 状态文件（临时文件 + rename 原子写入）：

- `locksvc.json`：`counter`（已发最大令牌）+ 各锁租约；
- `resourcesvc.json`：每个资源的值与已见令牌。

进程重启时从同一目录恢复，因此**令牌计数与已见令牌都不会回退**——这正是围栏令牌在进程崩溃场景下仍然安全的关键。

## 目录结构

```
clock/            可注入时钟（Real / Fake）
store/            JSON 状态文件原子读写
locksvc/          锁服务核心 + HTTP handler + 单元测试
resourcesvc/      资源服务核心 + HTTP handler + 单元测试
integration/      端到端验收测试（httptest + 手动时钟，含重启）
cmd/server/       启动两个 HTTP 服务的 main
scripts/demo.sh   真实服务器 + curl 的端到端演示
internal/apiutil/ 共享 JSON 读写辅助
```

## 测试与演示的实际运行结果（2026-09-24，go1.23.4 linux/amd64）

`go test -race ./...` 全部通过：

```
ok  fencingdemo/integration   TestAcceptanceFencingScenario（完整验收场景）
ok  fencingdemo/locksvc       5 个用例（获取/续租/释放/过期发新令牌/重启计数不回退）
ok  fencingdemo/resourcesvc   3 个用例（围栏写拒绝/令牌必填/重启已见令牌不回退）
```

集成测试日志确认旧持有者写入被拒绝：`旧持有者写入被拒绝: map[error:stale fencing token: write rejected seen:2]`。

`scripts/demo.sh` 对真实运行的服务器实测输出（节选）：

- A 获取锁 → `token: 1`；租约内 B 抢锁 → `HTTP 409`；
- 等待过期后 B 获取锁 → `token: 2`；B 写入 → `HTTP 200`；
- A 用令牌 1 迟到写入 → `HTTP 409 {"error":"stale fencing token: write rejected","seen":2}`；
- 资源内容保持 `value-from-B / token 2`；
- **真实进程重启后**（同一 `-state-dir`）：`/v1/counter` 返回 `2`（不回退），旧令牌 1 写入仍 `409`，C 获取锁得到 `token: 3`。

## 未完成项 / 已知限制

- 单进程模拟：两个组件同进程、不同端口，未做真正的网络分区/多副本；租约过期判断是请求时惰性检查，没有后台过期清理协程。
- 持久化为单文件 JSON 全量写，适合演示规模，非生产级存储。
- 围栏语义按"每个资源独立记录已见令牌"实现；跨资源的全局令牌水位未做（本场景不需要）。
- 未提供 TLS / 认证，仅监听回环地址，仅供本地演示。
