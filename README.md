# 可升级合约存储布局检查器（Solidity + Foundry + FastAPI + web3.py）

从空目录实现的“可升级存储布局检查”项目：

- **链上样例**：自实现的 ERC-1967 UUPS 代理（`src/proxy/`）+ 6 个逻辑版本（`src/v1..v6`），覆盖字段追加、重排、类型扩宽、动态类型变更、基类变更（安全/不安全）。
- **检查器**（`checker/`）：输入两版编译器布局 JSON（solc `storageLayout` 格式），识别槽位、偏移、继承与动态类型变化，输出 `compatible` 判定与逐条 findings。
- **HTTP 服务**（`api/`）：FastAPI，连接本机 Anvil（HTTP），提供布局检查、部署、受检查保护的升级、状态读取端点。
- **链上验证**：web3.py 部署 V1 → 写入全部存储类型 → 升级到 V2 → 断言原数据保持；不兼容版本在升级前被检查器拦截。

## 目录结构

```
src/
  proxy/ERC1967Proxy.sol        # 自实现 ERC-1967 代理（fallback delegatecall）
  proxy/ERC1967Upgrade.sol      # EIP-1967 实现槽读写
  core/UUPSUpgradable.sol       # UUPS 升级入口 + owner（槽 0）+ 49 槽 gap
  core/InitialStorageV0.sol     # 基类 V0（含 10 槽预留 gap）
  core/InitialStorageV1.sol     # 基类 V1：baseCounter uint64→uint256（不安全）
  core/InitialStorageV2.sol     # 基类 V2：消费 1 槽 gap 追加 baseName（安全）
  v1/BoxV1.sol                  # 初始版本：packed 字段、string、bytes、动态数组、mapping
  v2/BoxV2.sol                  # 兼容：仅追加 extra/a/b
  v3/BoxV3.sol                  # 不兼容：字段重排
  v4/BoxV4.sol                  # 不兼容：y uint32→uint256；counts 数组→mapping
  v5/BoxV5.sol                  # 不兼容：换用 InitialStorageV1（基类字段扩宽）
  v6/BoxV6.sol                  # 兼容：换用 InitialStorageV2（基类 gap 演进）
checker/__init__.py             # 布局比较核心（规则见模块 docstring）
checker/chain.py                # web3.py 部署/升级/状态读写
api/main.py                     # FastAPI 服务
script/export_layouts.py        # solc standard-json 编译并导出 layouts/*.json
script/demo_upgrade.py          # 端到端演示（检查 + 部署 + 升级 + 数据保持）
layouts/                        # 导出的布局包（storageLayout + abi + bytecode + 继承线性化）
tests/unit/                     # 离线检查器测试（19 例）
tests/integration/              # Anvil 链上升级测试
tests/api/                      # FastAPI 测试（含链上流程）
```

## 依赖与启动

环境：Linux，Python 3.12，Foundry（forge/anvil/cast，solc 0.8.24 由 forge 缓存到 `~/.svm`）。

```bash
# 1. 安装 Foundry（一次性）
curl -L https://foundry.paradigm.xyz | bash && foundryup

# 2. Python 依赖（锁定版本见 requirements.lock）
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock

# 3. 编译合约并导出布局包
forge build
python script/export_layouts.py

# 4. 启动本机链（另一个终端）
anvil --silent

# 5. 启动 HTTP 服务（另一个终端；8000 被占用时换 --port）
. .venv/bin/activate
uvicorn api.main:app --port 8000

# 6. 运行测试
pytest -q                    # 全部（unit + api + integration）
pytest tests/unit -q         # 仅离线检查器测试

# 7. 端到端演示
python script/demo_upgrade.py
```

链环境只用本机 Anvil 与公开测试私钥（Anvil 账户 #0，`checker/chain.py` 中 `ANVIL_KEY_0`），HTTP 接口只连 `http://127.0.0.1:8545`。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 服务与链连通性 |
| GET | `/contracts` | 可用布局包列表 |
| POST | `/check` | 上传两份布局 JSON 进行比较 |
| POST | `/check/named` | 按名称比较 `layouts/` 下两个版本 |
| POST | `/deploy` | 部署实现 + ERC1967 代理，可选写入演示数据 |
| POST | `/upgrade` | 先跑检查器；不兼容直接拒绝（`blocked: true`），兼容则升级并对比升级前后状态 |
| GET | `/proxy/{addr}/state` | 读取代理当前状态 |

示例：

```bash
curl -s localhost:8000/check/named -H 'content-type: application/json' \
  -d '{"old":"BoxV1","new":"BoxV3"}' | jq '.compatible, .errors[0].code'
# false  "SLOT_MOVED"

curl -s localhost:8000/deploy -H 'content-type: application/json' -d '{"impl":"BoxV1"}'
curl -s localhost:8000/upgrade -H 'content-type: application/json' \
  -d '{"proxy":"<addr>","new_impl":"BoxV2","old_impl":"BoxV1"}' | jq '.state_preserved'
# true
```

## 检查规则（`checker/__init__.py`）

对两份 `storageLayout` 文档按 label 配对，以下情形产生 **error 并阻止升级**：

1. **槽位/偏移移动**（`SLOT_MOVED`/`OFFSET_MOVED`）——字段重排、基类字段扩宽导致的打包位移；
2. **类型变化**（`TYPE_CHANGED`/`DYNAMIC_TYPE_CHANGED`）——比较编码（inplace/dynamic_array/mapping/bytes）、字节数、数组元素类型、mapping 键值类型、struct 成员；`uint32→uint256`、`uint256[]→mapping` 均被拦截；
3. **删除变量**（`VAR_REMOVED`）；
4. **新增变量与旧变量槽位冲突**（`SLOT_COLLISION`）——新变量只能追加到尾部或消费预留 gap；
5. **gap 非法演进**（`GAP_MOVED`/`GAP_GREW`）——gap 可原地收缩，且头部前移必须被新变量完全消费（`GAP_CONSUMED`，info）。

继承/基类变更通过**逐槽结构化比较**捕获（solc 0.8.24 会把继承字段归属到叶子合约，因此不能依赖 `contract` 字段）；若两版 `linearizedBaseContracts` 不同，额外给出 `INHERITANCE_CHANGED` 警告。`layouts/*.json` 中的继承线性化列表由 `script/export_layouts.py` 从 AST 导出。

## 验收结果（实际运行记录）

环境：Ubuntu（Linux 6.8），forge/anvil 1.8.3，solc 0.8.24，Python 3.12.3。

- `pytest -q`：**28 passed**（unit 19 + api 7 + integration 2），含链上部署 V1→写入→升级 V2→数据保持断言。
- `python script/demo_upgrade.py`：5 个候选版本判定全部符合预期（V2/V6 兼容，V3/V4/V5 拦截）；V1→V2 升级后 `x/packed/name/flags/counts/values/baseCounter` 全部保持。
- 测试过程中修复的真实缺陷：`/upgrade` 端点把 `CheckResult` 对象当 dict 用（被 API 测试捕获）；`BoxV2.setAB` 写成 `b = b`（编译器警告捕获）。

## 已知限制 / 未完成项

- 检查器以**同 label 配对**为基础；字段改名会被视为“删除+新增”（保守地报错），未实现重命名识别。
- solc 0.8.24 的布局 JSON 中 `contract` 字段全部归一为叶子合约名，基类归属信息不可直接利用；基类变更依赖结构化比较 + 线性化列表警告，未做“哪个基类定义了哪个字段”的精确归因。
- 未实现 transient storage（EIP-1153）布局、未处理 `immutable`/`constant`（它们不进存储，solc 布局中本就不出现）。
- `force=true` 可绕过检查器执行升级（便于演示“拦截”语义），生产环境应移除。
- 未接 CI；未对 API 做鉴权（本机演示用途）。
