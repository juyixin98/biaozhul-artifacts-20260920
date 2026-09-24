# 迁移失败恢复手册（Rollback & Recovery Runbook）

本文件说明存储版本迁移失败时如何判断、止损和恢复。所有命令均由
`scripts/migrate-storage.sh` 和 `cmd/migrator` 实现，可在真实集群直接执行；
集成测试 `TestStorageMigrationAndFailureRecord` 完整走查了本手册的路径。

## 1. 背景：什么时候会失败

升级（v1alpha1 → v1）在本项目中**总是可表达**的：整数秒可以无损放入
`{seconds, nanos}`。失败只可能来自基础设施（webhook 不可达、apiserver 5xx、
资源冲突超过重试上限）。

降级（v1 → v1alpha1）存在**语义不可表达**的对象：任何 `nanos != 0` 的
duration。转换 webhook 对此**明确报错，绝不静默截断**：

```
spec.interval={seconds:0,nanos:500000000} cannot be expressed in v1alpha1
(whole seconds only); sub-second precision must be rounded explicitly before
downgrade, it is never truncated implicitly
```

这就是 `fixtures/migration/migration-down-failed.json` 记录的真实失败。

## 2. 失败时系统的保证

- 迁移器逐对象处理：一个对象失败不阻断其余对象的扫描；
- 无论成功或失败，记录文件都会**原子落盘**（`*.tmp` 后 rename），包含
  `total/migrated/skipped/failed` 与每个对象的 `error/attempts/durationMs`；
- 迁移器**从不自动修改** `status.storedVersions`；
- CRD 同时保留两个 served 版本，失败期间对象仍可通过其原始存储版本读写；
- 转换 webhook 为无状态服务，可随时重启/回滚 Deployment，不改变 etcd 数据。

## 3. 失败判定

```sh
# 迁移器退出码非 0 即失败；记录里 failed>0
go run ./cmd/migrator --direction=down --record=migration.json
jq '.failed, (.objects[] | select(.status=="failed"))' migration.json
```

记录示例（真实测试产物）见 `fixtures/migration/migration-down-failed.json`。

## 4. 恢复流程

### 4a. 语义失败（亚秒值无法降级）

1. 用记录定位对象：`namespace/name` 与具体字段；
2. **做一个明确的数据决策**并记录，例如：
   - 业务允许取整：由具备 v1 写权限的客户端显式改为整秒（必须由人或有
     明确业务授权的控制器决定舍入规则，迁移器不替你做）；
   - 保留精度：不降级该对象，维持 v1 为存储版本；
3. 重新运行 `scripts/migrate-storage.sh down`，记录文件追加（新时间戳）；
4. 新记录 `failed==0` 后才允许改写 storedVersions。

### 4b. 基础设施失败（webhook 超时、连接拒绝、apiserver 5xx）

1. 确认 webhook 健康：`kubectl -n timer-system logs deploy/timer-webhook`；
2. 迁移器对冲突有最多 3 次退避重试；持续性失败需先修 webhook；
3. webhook 修好后**直接重跑同一方向命令**——已迁移对象的重复更新是幂等的
   （读取后原样写回，只刷新 resourceVersion）；
4. 检查记录中 `attempts` 与 `durationMs`，区分偶发与系统性问题。

### 4c. 转换 webhook 整体不可用

- 短期止损：不要删除 webhook 配置（删除会让带亚秒值对象以损坏形态降级）；
- 回滚 webhook 镜像即可；转换逻辑纯函数式、无外部依赖，旧版本镜像与新 CRD
  schema 兼容（两版本均继续 served）。

## 5. 何时才能移除旧版本

只有同时满足：

1. 该方向最新记录 `failed == 0`；
2. 所有对象已按新存储版本重写（记录里逐对象 `status == "migrated"`）；
3. 已在测试/预发环境演练过反向回滚；

才执行（示例：移除 v1alpha1）：

```sh
kubectl patch crd timers.timer.example.com --subresource=status --type merge \
  -p '{"status":{"storedVersions":["v1"]}}'
```

该步骤是脚本成功后**手动确认**的一步，故意不自动化。
