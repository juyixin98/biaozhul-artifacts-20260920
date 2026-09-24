# 证书续期协调 (Certificate Renewal Coordinator)

一个**纯后端**的 Kubernetes 控制器：对自定义资源 `Certificate` 所声明的域名，
使用**本地测试 CA** 通过 Go 标准库 `crypto/x509` 真实签发 TLS 证书、写入
`kubernetes.io/tls` 类型 Secret，并把**续期窗口与证书自身的有效期绑定**，在临近
到期时自动续期。所有密码学运算（ECDSA P-256 密钥生成、PKCS#10 CSR、X.509 签发、
链与 SAN 校验）均真实执行；测试 CA 仅供本地样例，切勿用于生产。

- 语言/库：Go 1.22、`crypto/x509`、`sigs.k8s.io/controller-runtime`
- 运行环境：[kind](https://kind.sigs.k8s.io/)（Kubernetes IN Docker）
- 无任何前端页面。

---

## 1. 安全语义（本项目要保证的核心不变式）

| # | 语义 | 实现位置 |
|---|------|----------|
| 1 | **续期窗口绑定证书有效期**：续期时刻 = `notAfter - renewBefore - 时钟偏差(5m)`，由已签发证书的真实 `NotAfter` 推导，而非墙上定时器 | `internal/pki/pki.go` (`RenewalTime`,`NeedsRenewal`)、`internal/controller/reconcile.go` |
| 2 | **Secret 内容、序列号、状态摘要三者一致**：都来自同一份被解析的证书，并在写库后 **read-back** 再次用 x509 校验 | `reconcile.go` 提交后读回 + `internal/controller/helpers.go` |
| 3 | **签发失败不删除旧证书**：失败路径完全不触碰正在服务的 Secret；旧证书仍有效时 `Ready=True`，仅 `Issuing`/失败原因更新 | `reconcile.go` 失败分支 |
| 4 | **重复调谐复用未完成申请**：私钥+CSR 持久化在 `<secret>-pending`，重试/重启复用同一密钥，不再每次新造 | `reconcile.go` pending 处理 + `pki.IssueWithKey` |
| 5 | **旧代签发回执不能覆盖新域名配置**：签发返回后、提交前重新读取 live CR，generation 或 specHash 变化则丢弃回执并按新配置重签 | `reconcile.go` 的 COMMIT GATE |
| 6 | **私钥不写日志**：私钥只进入 Secret 的 `tls.key`；日志仅含序列号/时间等元数据（e2e 会扫描日志确认无 `PRIVATE KEY`） | 全局日志策略 |
| 7 | **Secret 更新冲突可恢复**：提交用 read-modify-write + 乐观锁重试（默认 5 次），他人注解在重试中保留 | `commitSecretWithRetry` |
| 8 | **防止"签发即续期"热循环**：校验 `renewBefore + skew < duration`，保证新证书有严格为正的健康期 | `api/v1alpha1/spec.go` |

时钟偏差容忍：新证书 `NotBefore` 回拨 5 分钟，`VerifyLeaf` 以注入时钟校验，
±5m 内的控制器/CA 时钟偏差不会造成误判；续期时刻再额外提前 5 分钟。

---

## 2. 目录结构

```
api/v1alpha1/            CRD Go 类型、spec 归一化/默认值/校验、specHash
internal/pki/            真实 crypto/x509：测试 CA、密钥、CSR、签发、链/SAN 校验
internal/issuer/         用本地 CA 实现 controller.Signer
internal/controller/     Reconciler（续期、pending、提交闸门、冲突重试、状态一致）
cmd/manager/             控制器进程
cmd/gentestca/           生成自签测试 CA Secret 清单（样例工具）
config/crd|rbac|manager|samples/   CRD、RBAC、Deployment、示例 Certificate
hack/e2e-acceptance.sh   kind 真集群验收（openssl verify 真实校验链与域名）
```

---

## 3. CRD 示例

```yaml
apiVersion: certificates.example.com/v1alpha1
kind: Certificate
metadata:
  name: example-app
spec:
  dnsNames: ["example.app.local", "www.example.app.local"]
  secretName: example-app-tls        # 写入 kubernetes.io/tls Secret
  duration: 24h                      # 可选，默认 24h；最小 11m
  renewBefore: 8h                    # 可选，默认 duration/3；须 < duration-5m
  issuer:
    name: test-ca                    # 持有 tls.crt/tls.key 的 CA Secret
```

状态摘要（`status`）：`serial`、`thumbprint`(SHA-256)、`notBefore/notAfter`、
`renewalTime`、`specHash`、`currentSecret` 与条件 `Ready`/`Issuing`。
Secret 注解 `certificates.example.com/{serial,thumbprint,spec-hash,generation,not-after}`
与状态保持一致。

---

## 4. 本地启动

前置：`go 1.22+`、`docker`、`kind`、`kubectl`、`openssl`。

### 4.1 一键（推荐）

```bash
make e2e          # 创建 kind 集群 -> 构建/加载镜像 -> 部署 CRD/RBAC/控制器 -> 部署样例 -> 跑验收
```

### 4.2 分步

```bash
# 1) 自动化单元 + 集成测试（内存假 apiserver，含乐观锁冲突；-race）
make test

# 2) 生成测试 CA Secret 清单（真实生成 ECDSA CA；私钥仅进入清单的 tls.key）
make ca            # -> config/samples/generated/test-ca-secret.yaml

# 3) kind 集群、镜像、部署
make kind-up
make image
make deploy

# 4) 部署 CA 与样例证书
make samples

# 5) 查看
kubectl get certificates
kubectl get secret example-app-tls -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -text
```

### 4.3 进程外本地运行（不用容器镜像）

```bash
kind create cluster --name cert-renewal
kubectl apply -f config/crd/ -f config/rbac/ -f config/namespace.yaml
make ca && kubectl apply -f config/samples/generated/test-ca-secret.yaml
go run ./cmd/manager --kubeconfig "$HOME/.kube/config"
```

---

## 5. 验收命令

### 5.1 单元 / 集成（无需集群）

```bash
go test -race -count=1 ./...
```

覆盖：真实链与域名校验、**临近到期**、**时钟偏差**、**Secret 更新冲突**、
**spec 域名变更**、**旧代回执丢弃**、签发失败保留旧证书、**重复调谐与控制器重启
复用 pending 申请**、CA 缺失、非法 spec。

### 5.2 kind 真集群端到端

```bash
make e2e
# 或在已有集群（已 make deploy 且 kubeconfig 指向 kind）上：
./hack/e2e-acceptance.sh
```

脚本会真实执行（成功时输出 `=== ALL ACCEPTANCE CHECKS PASSED ===`）：

1. `openssl verify -CAfile ca.crt -verify_hostname <每个SAN>` 校验**证书链与域名**；
2. 比较 `tls.crt` 公钥与 `tls.key` 公钥，确认配对；
3. 断言 Secret 注解序列号/指纹 == `status.serial/thumbprint` == 解析证书；
4. 等待**真实续期窗口**（20m/14m → 签发后约 1 分钟）序列号轮换，并对新链再次 `openssl verify`；
5. 修改 `dnsNames`，确认新证书真实携带并通过新域名校验；
6. 删除 CA 模拟签发失败，断言**不产生半成品 Secret、不改动仍有效旧 Secret**。

可快速肉眼观察续期：

```bash
kubectl get certificates --watch
# 短生命样例（健康期约 1 分钟）
kubectl apply -f config/samples/certificate-short-lived.yaml
```

---

## 6. 失败如实报告

- 签发/校验失败不会返回致命错误导致热重启，而是以条件形式上报：
  `Ready=False, reason=IssuanceFailed|ValidationFailed|CAUnavailable|SpecInvalid`，
  消息含真实错误（如"requested validity extends beyond CA expiry"）。
- 旧证书仍有效时，续期失败保持 `Ready=True`，`status` 继续摘要旧证书。
- 状态更新与 Secret 提交均对 `409 Conflict` 做有界重试；超限返回真实错误并重排队。

---

## 7. 依赖

依赖经 `go mod tidy` 锁定于 `go.sum`（见 `go.mod`：k8s.io/* 与
controller-runtime v0.18.x，对应 Kubernetes 1.30）。`go test` 不依赖任何
额外下载的二进制（使用 controller-runtime 内存 client）；kind e2e 仅需 docker/kubectl/openssl。

## 8. 安全提示

`cmd/gentestca` 生成的是**自签测试 CA**，仅用于本地开发/演示。控制器以最小
RBAC 运行（仅 Certificate 及其 status、Secrets、events），容器
`runAsNonRoot`、只读根文件系统、drop ALL capabilities。
