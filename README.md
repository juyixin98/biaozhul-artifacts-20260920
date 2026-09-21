# SiteVitals

SiteVitals 在**内网/本地**用真实的无头 Chromium（Chrome DevTools Protocol）采集网站性能：导航耗时、FCP、LCP、CLS、长任务、资源瀑布。全部组件（API、worker、MySQL、Chromium、内置演示站点）都在本地/Docker 内运行，**不依赖任何云服务**。

技术栈：Go 1.22 · Gin · GORM · MySQL 8 · Chromium（[chromedp](https://github.com/chromedp/chromedp)）。

---

## 1. 快速开始（Docker）

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 |
| --- | --- |
| API | http://127.0.0.1:18092 |
| 内置演示站点（容器内/已映射） | http://127.0.0.1:18093 |
| MySQL | 127.0.0.1:13396（root/sitevitals，库名 `sitevitals_b`） |

`--testsite` 会自动启动演示站点并幂等写入一条站点登记 + 白名单规则（`http://127.0.0.1:8093/*`）。

发起一次采集（移动视口）：

```bash
curl -s -X POST http://127.0.0.1:18092/api/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{"url":"http://127.0.0.1:8093/","viewport":"mobile"}'

# 查询任务与成功运行（含指标、瀑布、事件）
curl -s http://127.0.0.1:18092/api/v1/jobs/1
curl -s http://127.0.0.1:18092/api/v1/runs/<run_id> | jq .
```

演示站点内置的路径（用于触发各类场景）：

| 路径 | 用途 |
| --- | --- |
| `/` | 普通页（CSS/JS/PNG 瀑布） |
| `/slow?ms=1500` | 服务端延迟 |
| `/longtask` | 两个 >50ms 长任务 |
| `/cls` | 延迟插入元素，产生 CLS |
| `/missing` | 子资源 404（部分失败但运行成功） |
| `/redirect` | 默认 7 跳，超过 5 跳限制（`/redirect?n=4` 为 2 跳成功） |
| `/external` | 引用白名单外来源，子资源必须被拦截 |
| `/hang` | 永不完成，触发导航超时 |

## 2. 本地直接运行

需要本机有 MySQL 8 与 Chrome/Chromium：

```bash
# 1. 建库迁移（也可在 serve 时自动执行）
mysql -uroot -e "CREATE DATABASE IF NOT EXISTS sitevitals_b CHARACTER SET utf8mb4;"
go run ./cmd/sitevitals migrate

# 2. 启动 API + worker + 演示站点
go run ./cmd/sitevitals serve --testsite --testsite-addr :8093
# API 默认 :8092（SV_HTTP_ADDR），自动用本机 google-chrome/chromium
```

可用环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SV_HTTP_ADDR` | `:8092` | API 监听地址 |
| `SV_MYSQL_HOST/PORT/USER/PASSWORD/DB` | `127.0.0.1/3306/root//sitevitals_b` | MySQL 连接 |
| `SV_MYSQL_DSN` | （空） | 给定后覆盖上面拼装的 DSN |
| `SV_WORKERS` | `4` | 并发 worker 数 |
| `SV_MAX_BROWSERS` | `2` | **同时运行的 Chromium 进程上限**（信号量，低于 worker 数时 worker 排队等浏览器并持续续租） |
| `SV_LEASE_TTL` | `30s` | 租约有效期 |
| `SV_HEARTBEAT_INTERVAL` | `10s` | 续租心跳间隔 |
| `SV_REAP_INTERVAL` | `5s` | 回收过期租约的扫描间隔 |
| `SV_MAX_REDIRECTS` | `5` | 最大重定向跳数 |
| `SV_NAV_TIMEOUT` | `45s` | 单次导航硬超时 |
| `SV_CHROME_BIN` | 自动探测 | Chromium 可执行文件路径 |
| `SV_TESTSITE_ADDR` | `:8093` | 内置演示站点监听地址 |

## 3. 数据模型与任务队列

```
sites 1──* allowed_urls          登记允许访问的站点（origin）与精确/前缀(以 * 结尾)URL 规则
jobs 1──* runs 1──* metrics      一个 job 可有多次 attempt(run)；仅一次成功 run 产出报告
                 ├─ resource_entries   资源瀑布（Document/CSS/JS/Image…，start/end/duration/字节数）
                 └─ run_events         重定向、拦截、部分失败等事件
budgets 1──* budget_evaluations  预算阈值定义与每次成功运行的实际值/阈值/关联 run 评估
```

### 租约与崩溃恢复（fencing token）

- 领取：`SELECT … FOR UPDATE SKIP LOCKED` 取最早的 `queued` job，事务内置 `running`，`attempts+1`，`fencing_token+1`，并建一条对应的 `runs` 行。多个 worker 并发领取互不阻塞、不会重复领取。
- 心跳：执行期间按 `SV_HEARTBEAT_INTERVAL` 续租；心跳发现自己的 `(fencing_token, holder)` 已不匹配即收到 `ErrLeaseLost`，取消浏览器采集。
- 回收：reaper 周期把 `leased_until < now` 的 running job 重置为 `queued`（旧 run 标记为 `browser_exited` 失败），下一个领取者拿到**更大的 fencing token**。
- 提交防旧：成功/失败提交的 WHERE 都带 `fencing_token + lease_holder + status`。旧执行者迟到的成功提交是**空操作**——既不能覆盖新结果，也不会写出第二份报告（`succeeded_run_id IS NULL` 是成功提交的前置条件，`(job_id, attempt)` 唯一）。
- 重试：失败且 `attempts < max_attempts` 时回到队列；达到上限后置 `failed`。默认 `max_attempts=3`（建 job 时可传 `max_attempts`）。
- 不堵队：单个 job 失败只影响它自己；worker 取空队列时退避 2s 轮询。

## 4. 白名单策略

- 登记的 site origin 只允许 `http/https`；白名单按 (scheme, host, 精确路径 或 以 `*` 结尾的前缀，含 query) 匹配。
- 入队 API 先校验，**采集器执行时再校验一次**（策略可能已变更）。
- Chromium 开启 CDP `Fetch` 域在 **Request 与 Response 两个阶段**拦截每一个请求：
  - 非 http(s)、白名单外的主文档 → 整个 run 记 `policy_violation`；
  - 白名单外的子资源 → `FailRequest(AccessDenied)`，计入 `blocked_resources` 并在瀑布中记 `blocked`，页面本身继续加载；
  - 重定向链在 Response 阶段逐跳计数，超过 `SV_MAX_REDIRECTS` → `FailRequest` 并记 `policy_violation / too_many_redirects`。
- `file:`、`data:`、`javascript:`、内网其它端口、外网域名（如 `example.com`）默认全部拒绝。

## 5. 指标采集与诚实性

通过注入 `addScriptToEvaluateOnNewDocument`（早于任何页面脚本）安装 buffered `PerformanceObserver`，在 load 事件后再留一个 settle 观察窗（默认 2s）捕捉迟到的 LCP/CLS/长任务：

| 指标 | 来源 |
| --- | --- |
| `nav_ttfb_ms` / `nav_dcl_ms` / `nav_load_ms` / `nav_dom_complete_ms` | Navigation Timing Level 2 |
| `fcp_ms` | Paint Timing（`first-contentful-paint`） |
| `lcp_ms` | `largest-contentful-paint` buffered observer |
| `cls` | `layout-shift` observer 累积值（排除 `hadRecentInput`） |
| `long_tasks` | `longtask` observer（见下） |
| 资源瀑布 | CDP Network 域（monotonic 时基，相对主文档请求偏移） |

**不支持/采集失败绝不补零**：每个指标都有 `status`：

- `collected`：有真实值（`value_ms` 或 `value_cls`）；
- `unsupported`：浏览器/API 不支持（值列为 NULL，`detail` 说明原因）；
- `failed`：API 支持但观察窗内没有拿到（如无 LCP 条目）。

**长任务统计窗口**：从导航开始（observer 在新文档脚本之前安装，`buffered:true`）到 load 事件后 settle 窗结束（提取时刻 `performance.now()` 记录在 detail 的 `window_end_ms`）；阈值 **50ms**。detail JSON 记录窗口起止、任务条数、总时长、最长任务与每个任务的 `{start_ms, duration_ms}`。

失败分类（`runs.fail_class`，三类严格区分）：

| class | 触发条件 |
| --- | --- |
| `navigation_timeout` | 导航/load 未在 `SV_NAV_TIMEOUT` 内完成 |
| `browser_exited` | Chromium 启动失败、运行中崩溃/被杀、CDP 连接断开（含崩溃恢复测试钩子） |
| `collector_error` | CDP 设置/取值/解析失败 |
| `policy_violation` | 非 http(s)、白名单命中失败、重定向超限 |

子资源 404/失败不算 run 失败：run 成功，同时 `resource_failures>0`、瀑布行 `status=failed` 并产生 `partial_resource_failure` 事件。无论成功失败，Chromium 进程组（`Setpgid` + SIGKILL 整组）和临时 user-data-dir 都会在返回前清理。

## 6. 对比与预算告警

对比同 URL + 同视口最近两次**成功**运行：

```bash
curl -s 'http://127.0.0.1:8092/api/v1/compare?url=http://127.0.0.1:8093/&viewport=mobile' | jq .
```

返回 `older/newer`（含 run_id、job_id、各指标状态与值）和逐指标 `deltas`（差值、是否退化）。任一侧指标缺失（非 collected）时 `comparable=false`，不做升降结论；失败运行不参与对比（查询只取 `status=succeeded`）。

预算（可按 URL + 视口，视口为空表示全部）：

```bash
curl -s -X POST http://127.0.0.1:8092/api/v1/budgets -H 'Content-Type: application/json' -d '{
  "target_url":"http://127.0.0.1:8093/longtask",
  "viewport":"desktop",
  "metric":"fcp_ms",
  "threshold_ms":50
}'
```

成功 run 提交后自动评估：`budget_evaluations` 记录 **实际值、阈值、关联的 run_id/job_id、是否超限**；worker 日志打印 `BUDGET ALERT`。指标缺失时评估行 `skipped=true`（不与 0 比较、不算通过也不算超限）。只有成功 run 会触发评估，**失败运行不混入任何成功指标统计**。

## 7. HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 健康检查（含 DB ping） |
| POST/GET | `/api/v1/sites` | 登记/列出站点 |
| POST/GET | `/api/v1/sites/:id/allowed-urls` | 管理白名单规则 |
| POST | `/api/v1/jobs` | 入队（非 http(s) 或非白名单 → 403） |
| GET | `/api/v1/jobs?limit=N` | 任务列表 |
| GET | `/api/v1/jobs/:id` | 任务详情（状态、租约、fencing token、成功 run） |
| GET | `/api/v1/jobs/:id/runs` | 全部 attempt（失败原因分类） |
| GET | `/api/v1/runs/:id` | 指标 + 瀑布 + 事件 |
| GET | `/api/v1/compare?url=&viewport=[&run_id=]` | 两次成功运行对比 |
| POST/GET | `/api/v1/budgets[?url=]` | 预算管理 |
| GET | `/api/v1/budget-evaluations[?run_id=]` | 评估记录 |

视口取值固定为 `mobile`（390×844@3x，移动端模拟）、`tablet`（820×1180@2x）、`desktop`（1366×768@1x）。

## 8. 数据库迁移

- 版本化 SQL：`internal/migrate/0001_init.sql`（`embed` 进二进制），记录表 `schema_migrations`；`serve` 启动自动执行，也可单独：
  ```bash
  go run ./cmd/sitevitals migrate            # 应用未执行的迁移
  go run ./cmd/sitevitals serve --migrate-only
  go run ./cmd/sitevitals serve --no-migrate # 跳过
  ```
- 测试使用 GORM AutoMigrate 建库（长 URL 的前缀索引由版本化 SQL 负责，GORM 标签不声明这类索引以避免 MySQL 3072 字节限制）。

## 9. 测试

```bash
# 需要本机 MySQL（默认 root 无密码；各测试包使用独立 schema，可用 SV_TEST_DSN 覆盖）
mysql -uroot -e "
CREATE DATABASE IF NOT EXISTS sitevitals_b_store_test CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS sitevitals_b_worker_test CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS sitevitals_b_budget_test CHARACTER SET utf8mb4;"

go test ./... -timeout 300s
```

覆盖：

- `internal/policy`：非 http(s)、白名单外、主机混淆、精确/前缀规则、重定向跳数限制；
- `internal/store`：**租约竞争**（5 个并发领取者各得不同 job，SKIP LOCKED）、**崩溃恢复与防旧**（租约过期→回收→旧 fence 的心跳/迟到成功提交被拒→新 fence 成功，旧 run 不写指标，重复成功提交不产生第二份报告）、失败重试与 attempt 上限；
- `internal/worker`：坏任务 3 次失败不阻塞后续好任务、**真崩溃恢复**（首个 attempt 挂死且心跳长于 TTL，被 reaper 回收后由新 attempt 成功，旧 run 落 `browser_exited`）、执行时策略违规直接失败且不启动浏览器；
- `internal/collector`（**真实 Chromium**）：成功采集与瀑布、三种视口、外部子资源被拦截、**重定向限制**（7 跳链 vs 限制 5）、限制内重定向成功、导航超时分类与浏览器释放、部分子资源失败仍出指标、长任务、非 HTTP/非白名单拒绝；
- `internal/budget`：实际值/阈值/关联 run 落库、超限标记、缺失指标 `skipped` 不补零。

无 Chrome 或端口不可用时集成测试自动 `t.Skip`。

## 10. 已验证 / 未执行项

当前环境已用本机 **Google Chrome 138** 完成一次真实采集（见测试输出）：导航/FCP/LCP/CLS/长任务/瀑布均为 collected，长任务页可观察到 >50ms 任务。

未在本次会话内执行：

- 未执行 `docker compose build` 的完整镜像构建（环境 Docker 需 sudo，且已有同类容器占用端口）；Dockerfile/compose 按 debian + chromium 编写，可在有 Docker 权限的环境直接构建；
- 未做 HTTPS/证书类场景（本地演示为 HTTP；策略对 https origin 同等支持）；
- 没有接入鉴权/多租户——定位为内网单机工具，如需暴露网络请自行置于内网鉴权代理之后。
