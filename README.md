# 可升级合约存储布局检查器（Upgradeable Storage Layout Checker）

用 Solidity + Foundry 编写本地代理合约样例，用 Python + FastAPI + web3.py 实现
后端：输入两版 Solidity 编译器输出的 storage-layout JSON，识别**槽位、偏移、
继承关系及动态类型**变化，阻止不兼容升级；并在本机 Anvil 链上验证合法升级后
原有数据保持不变。

仅使用本机 Anvil 与公开测试密钥，HTTP 接口连接本地链（默认
`http://127.0.0.1:8545`），不触碰任何真实网络。

## 目录结构

```
contracts/
  UpgradeableProxy.sol      EIP-1967 风格最小代理（实现/管理员存于 1967 槽）
  BoxV1.sol / BoxV2.sol     合法升级样例：仅在末尾追加字段（含 mapping）
  bad/
    BoxBadReorder.sol       不兼容：字段重排（owner/value 互换槽位）
    WidenV1/V2.sol          不兼容：uint128 -> uint256 类型扩宽，挤压打包槽
    BaseV1/V2.sol           不兼容：新增基类 NewBase，在 x 前插入存储
    DynTypeV1/V2.sol        不兼容：uint256[] -> mapping(...) 动态类型变化
backend/
  layout_checker.py         核心：两版布局 JSON 的兼容性规则
  artifacts.py              读取 forge 编译产物（out/）及 out/bases.json
  chain.py                  web3.py：连接本地 Anvil、部署、签名发交易
  main.py                   FastAPI 服务（/check、/check-artifacts 等）
scripts/
  export_bases.py           构建后导出各合约 C3 线性化基类到 out/bases.json
  demo.py                   端到端演示：HTTP 检查 + Anvil 上实际升级
tests/                      pytest：单元 / API / Anvil 链上升级
```

## 依赖

- [Foundry](https://getfoundry.sh/)（forge 编译、anvil 本地链）。本机已装在
  `~/.foundry/bin`（forge/anvil 1.8.3，solc 0.8.20）；如未安装：
  `curl -L https://foundry.paradigm.xyz | bash && foundryup`
- Python 3.12；Python 依赖已锁定到 `requirements.txt`（fastapi 0.141、
  uvicorn 0.53、web3 8.0、pytest 9.1、httpx 0.28 等）。

## 启动与构建

```bash
# 1) Python 虚拟环境与依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 编译合约（foundry.toml 打开 storageLayout 输出）
export PATH="$HOME/.foundry/bin:$PATH"
forge build

# 3) 导出继承关系（检查 INHERITANCE_CHANGED 所需；构建后执行一次即可）
.venv/bin/python scripts/export_bases.py
```

### 启动 API 服务

```bash
# 另一个终端启动本地链（测试密钥固定，不要在真实网络使用）
anvil

# 启动检查器服务
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/layouts` | 列出 `out/` 中所有已编译合约 |
| GET | `/layouts/{name}` | 取某合约的编译器 storage-layout JSON |
| POST | `/check` | 提交两版原始布局 JSON（字段：`old_layout`、`new_layout`、可选 `old_name`/`new_name`、`old_bases`/`new_bases`） |
| POST | `/check-artifacts` | 直接按合约名比较（`{"old": "BoxV1", "new": "BoxV2"}`） |

示例：

```bash
curl -s -X POST http://127.0.0.1:8000/check-artifacts \
  -H 'content-type: application/json' \
  -d '{"old":"BoxV1","new":"BoxV2"}' | python3 -m json.tool
```

### 运行自动化测试

```bash
.venv/bin/pytest -v          # 单元 + API + Anvil 链上升级（自动起停 anvil）
```

### 运行端到端示例

```bash
.venv/bin/python scripts/demo.py   # 自动起停 anvil 和 API 服务，无需手动启动
```

## 检查规则（layout_checker.py）

只有 **error** 会阻止升级（响应里的 `compatible=false`）：

- `VARIABLE_REMOVED`：旧变量在新版中消失；
- `SLOT_MOVED` / `OFFSET_MOVED`：旧变量槽位或字节偏移变化（字段重排、插入、
  基类加字段、打包挤压都会命中）；
- `TYPE_CHANGED`：递归比较类型的 encoding、label、字节宽度，以及 struct 成员、
  数组基类型/长度、mapping 的 key/value——因此 `uint256[]` → `mapping`、
  `string` → `bytes`、struct 成员改类型都会被识别；
- `VARIABLE_INSERTED`：新变量落入旧布局占用的槽位范围。

允许的变化只有一种：新变量**追加**到旧布局之后（`VARIABLE_APPENDED`，info；
支持追加到最后一个槽的空闲打包偏移）。另有两条 warning：
`INHERITANCE_CHANGED`（基类线性化序列变化，数据来自 `out/bases.json`）和
`DUPLICATE_LABEL`（同名变量）。

变量匹配规则：按 label（变量名）匹配——变量重名/隐藏时可能漏判，这是静态
检查器的固有限制，重复 label 会给出 warning。

## 验收场景与实测结果

2026-09-24 在本机实测（Python 3.12.3，forge/anvil 1.8.3）：

| 场景 | 样例合约 | 结果 |
|---|---|---|
| 字段追加（uint256 + mapping） | `BoxV1 -> BoxV2` | 兼容，0 error |
| 字段重排 | `BoxV1 -> BoxBadReorder` | 阻止（SLOT_MOVED ×2） |
| 类型扩宽 | `WidenV1 -> WidenV2` | 阻止（TYPE_CHANGED / SLOT_MOVED / OFFSET_MOVED） |
| 基类变更（插入存储） | `BaseV1 -> BaseV2` | 阻止（VARIABLE_INSERTED / SLOT_MOVED，INHERITANCE_CHANGED 警告） |
| 动态类型变化 | `DynTypeV1 -> DynTypeV2` | 阻止（dynamic_array → mapping 等 TYPE_CHANGED） |
| 合法升级后数据保持 | proxy: BoxV1 → BoxV2 | `value=1337`、`name="hello-layout"`、`owner` 升级前后一致，新版 `version()=v2`、追加字段 `extra=7` 可写 |

- `pytest`：**27 passed**（12 个内联规则用例 + 5 个真实编译布局用例 + 8 个 API
  用例 + 2 个链上/门禁用例）。
- `scripts/demo.py`：5 组 HTTP 检查输出与上表一致；链上升级前后原数据保持，
  见仓库根目录 `DEMO_OUTPUT.txt`（完整运行记录）。

## 说明与未完成项

- 代理为教学用最小实现：无初始化器保护框架（BoxV1/V2 自带简单 initialize）、
  无函数选择器冲突处理，不可用于生产；生产请用 OpenZeppelin Upgrades。
- 检查器只做静态布局比较，不做选择器兼容性检查（如函数删除、事件变更）。
- 继承信息依赖构建后执行 `scripts/export_bases.py`（基于
  `forge inspect linearization --json`）；未生成 `bases.json` 时仍可比较槽位，
  只是不会给出 INHERITANCE_CHANGED 警告。
- 未做 CI 配置与 Docker 镜像（本项目只面向本机 Anvil 使用）。
