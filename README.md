# 证书续期协调 (Certificate Renewal Coordinator)

纯后端 Kubernetes 控制器，为自定义 `Certificate` CRD 持续保持一张由**本地测试 CA** 签发的 TLS 证书。使用 Go + `crypto/x509` + `sigs.k8s.io/controller-runtime`，并在 [kind](https://kind.sigs.k8s.io/) 上做端到端验收。

> ⚠️ 内置 CA 仅为**样例/测试用途**：密钥在本地即时生成，绝不能用于保护真实流量。

## 它解决什么

- **续期窗口与证书有效期绑定**：`renewalTime = notAfter - renewBefore`（钳制到 `notBefore`），不使用任何绝对墙钟计划；默认 `renewBefore = duration/3`。
- **时钟偏差只保护到期**：30s 容差施加在"是否已到期/即将到期"的判断上（防止节点时钟超前导致继续发过期证书），但**不**把续期触发点整体提前——否则 `renewBefore` 极接近 `duration` 时会让刚签发的证书立刻续期形成紧密循环。
- **Secret 内容、序列号、状态摘要三者一致**：写 Secret 后重新读取，状态中的 serial / notBefore / notAfter / renewalTime 全部从 Secret 里的真实证书解析得到。
- **签发失败保留仍有效的旧证书**：新链在**本地真实验证通过之前绝不写 Secret**，更不会先删除旧 Secret；旧证书有效期内 `Ready=True/Renewing` 继续服务。
- **重复调谐复用未完成申请**：申请名由 `(owner, specHash)` 确定性派生；并发生成/重启后都复用同一个 `CertificateRequest`。
- **旧代签发回执不能覆盖新域名配置**：申请带 `spec-hash` 注解；写 Secret 前再次用**当前 spec** 校验 leaf 的 SAN/CN 并独立验证证书链。旧申请保留作审计。
- **控制器重启安全**：在途私钥暂存在专用 `<request>-key` Secret（带标签，应用后删除），重启后可恢复，不会丢失在途申请或重复创建。
- **时钟偏差容忍**：续期判定带 30s 偏斜容差。
- **私钥不写日志**：有专门测试扫描全部日志行，确认不出现 PEM 私钥内容。

所有计算、协议与密码操作均真实执行：`crypto/x509.CreateCertificateRequest` / `CreateCertificate`（ECDSA P-256）、CSR 签名自校验、`x509.Certificate.Verify` 路径验证、以及端到端测试里的**真实 TLS 握手（SNI + 主机名校验）**。

## 架构

```
Certificate ──(创建/复用)──▶ CertificateRequest ◀──(签名,写 status)── CertificateRequest controller
      ▲                              │
      │                         pending <name>-key Secret (在途私钥, 应用后删除)
      └────────(验证链/SAN/密钥对后原子写)── TLS Secret (tls.crt, tls.key, ca.crt)
```

- `api/v1alpha1`：`Certificate`、`CertificateRequest` CRD 类型。
- `internal/pki`：密钥/CSR 生成、CA 签发、证书链验证、续期窗口数学、spec 指纹（无 mock）。
- `internal/ca`：从 TLS Secret 加载测试 CA（校验 IsCA、密钥与证书匹配）。
- `internal/controller`：两个调谐器。
  - `Certificate`：评估现有 Secret → 续期判定 → 复用/创建申请 → 应用回执（先验证后原子写）→ 状态汇总。
  - `CertificateRequest`：加载 CA → 校验 CSR 签名与**当前域名** → 真实签名写回 status；失败指数退避重试。
- `cmd/manager`：进程入口。
- `hack/gen-test-ca`：生成一次性测试 CA Secret 清单。

### 状态与条件

- `Certificate.status.conditions[Ready]`
  - `True / Reconciled`：Secret 中证书与 spec 一致且未进入续期窗口。
  - `True / Renewing`：续期在途，但旧证书仍有效并继续服务。
  - `False / Issuing|InvalidSpec`：首次签发未完成，或 spec 非法。
- `CertificateRequest.status.conditions[Ready]`
  - `True / Signed`：已签发，status 中含证书、CA、序列号、有效期。
  - `False / CAUnavailable|CSRInvalid`：签发失败，带 `failureCount` 与退避重试。

## 目录

```
api/v1alpha1              CRD 类型与 deepcopy
cmd/manager               控制器进程
internal/pki              真实密码学操作 + 续期数学（含单测）
internal/ca               测试 CA 加载
internal/controller       两个调谐器（含 10 个场景单测）
config/crd/bases          CRD 清单
config/rbac               ServiceAccount / ClusterRole / Binding
config/manager            Deployment
config/samples            示例 Certificate
hack/gen-test-ca          生成测试 CA Secret
test/e2e                  kind 端到端测试（真实 TLS 握手）, build tag: e2e
Dockerfile                多阶段离线构建（使用 vendor/）
Makefile                  常用命令
```

## 前置条件

- Go 1.22+
- Docker、[kind](https://kind.sigs.k8s.io/)、kubectl

## 本地启动（kind）

```bash
# 1) 依赖（已随 go.mod/go.sum 与 vendor/ 锁定）
go test ./... -count=1

# 2) 创建 kind 集群（已存在则跳过）
make kind-cluster

# 3) 构建镜像并载入 kind
make kind-load

# 4) 安装 CRD/RBAC/控制器并等待就绪
make deploy

# 5) 创建测试 CA（一次性样例密钥）与示例证书
make sample-ca
make samples

# 6) 观察
kubectl get cert,crq
kubectl get secret sample-web-tls -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -text | head -30
```

## 验收命令

```bash
# 单元测试（含临近到期、时钟偏差、Secret 更新冲突、spec 变更、控制器重启、
# 签发失败保留旧证书、私钥不写日志等；带 race detector）
make test

# kind 端到端（真实集群 + 真实证书链 + 真实 TLS 握手与域名校验）
make e2e
```

端到端包含：

1. `TestE2E_FullIssuanceAndTLSHandshake`：签发→Secret/序列号/状态一致→用 leaf 起本地 HTTPS，客户端以 `ca.crt` 为根、按 SNI 完成真实握手。
2. `TestE2E_SpecChangeRotatesDomains`：改域名后旧回执不落 Secret；新证书对新名校验成功、对旧名**失败**；旧申请保留。
3. `TestE2E_ShortLivedRenewsNearExpiry`：10m/9m 的短证书在约 1 分钟后真实进入续期窗口并换序列号，无中断。
4. `TestE2E_CAOutageKeepsServingThenRecovers`：删除 CA Secret 期间签发失败、旧证书持续服务且 `Ready=True`；恢复 CA 后复用在途申请完成续期。
5. `TestE2E_ControllerRestartCompletesInFlight`：把控制器 Deployment 缩 0 再扩 1，重启后状态与 Secret 保持一致。

也可以手工验收：

```bash
# 改域名，观察旧证书继续服务、新序列号出现
kubectl patch cert sample-web --type=merge -p '{"spec":{"dnsNames":["web2.example.com"],"commonName":"web2.example.com"}}'
kubectl get cert sample-web -w

# 验证真实证书链与域名（CA 在 default/test-ca）
kubectl get secret test-ca -o jsonpath='{.data.ca\.crt}' | base64 -d > /tmp/ca.crt
kubectl get secret sample-web-tls -o jsonpath='{.data.tls\.crt}' | base64 -d > /tmp/leaf.crt
openssl verify -CAfile /tmp/ca.crt /tmp/leaf.crt
openssl x509 -in /tmp/leaf.crt -noout -ext subjectAltName
```

## 示例输入

见 `config/samples/certificate_web.yaml`（24h，默认续期窗口 8h）与 `certificate_shortlived.yaml`（30m，提前 10m，可现场观看续期）。

> 配置提示：`renewBefore` 应明显小于 `duration`（经验值不超过有效期的 ~2/3）。若二者非常接近（如 10m/9m30s），续期点会落在签发后 30s，导致新证书很快再次续期——这是规范的数学结果而非故障，但在生产中无意义。

## 清理

```bash
make undeploy
kind delete cluster --name cert-renewal   # 可选
```

## 安全说明

- 私钥仅出现在：生成进程内存、在途暂存 Secret（`<request>-key`，应用后立即删除）、最终 TLS Secret。日志中只打印序列号与时间，不打印任何密钥材料（有测试守护）。
- RBAC 最小化：仅需 `certificates`/`certificaterequests`（含 status）、`secrets`、`events`。
- 容器以 nonroot、只读根文件系统、dropped capabilities 运行。
- 测试 CA 为样例设施，不做持久化、不提供 CRL/OCSP，请勿用于生产。
