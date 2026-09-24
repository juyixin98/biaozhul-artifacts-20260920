# 电池遥测状态估算服务（合成数据 / 纯后端 / 离线 SOC）

使用 **Python + NumPy + FastAPI** 实现的**合成电池**离线 SOC（荷电状态）估算服务：
库仑积分 + 可信静止窗口内按冻结 OCV 表校准，输出不确定度与数据质量标记。

> ⚠️ **安全声明**：本项目使用的参数（容量、OCV 表、温度系数、效率等）是**人为编造的合成
> 参数**，算法为教学/演示模型，**未经过任何真实验证，禁止用于真实电池的充电控制、保护
> 动作或任何安全相关决策**。所有计算真实执行、确定性可复现，但结果仅适用于合成数据。

---

## 1. 单位与符号约定（API 字段名显式携带单位）

| 字段 | 单位 | 说明 |
|---|---|---|
| `t_s` | UTC 纪元秒（float） | 采样时间戳 |
| `current_a` | A | **正 = 放电（SOC 下降），负 = 充电（SOC 上升）** |
| `voltage_v` | V | 电池端电压 |
| `temp_c` | °C | 电池温度 |
| `soc` | 无量纲 | SOC 分数，**限制在 [0, 1]**，越界会被钳位并标记 |
| `sigma` / `sigma_*` | 无量纲 | SOC 不确定度（保守上界，非经校准的统计区间） |
| `capacity_ah` / `eff_capacity_ah` | Ah | 标称容量 / 温度修正后的有效容量 |

## 2. 算法（`app/engine.py`，完全确定性，无随机数、无 I/O）

1. **库仑积分**：`ΔSOC = -I·dt·η / (3600·Q_eff(T))`
   - 充电 `η = 0.995`，放电 `η = 1.0`（冻结在参数清单中）；
   - `Q_eff(T) = Q_nominal · capacity_factor(T)`，按温度表线性插值；
   - **表外温度**：向最近边界钳位并打 `temp_out_of_table` 标记（-20°C 以下、55°C 以上）。
2. **数据缺口不是零电流**：当 `dt > gap_threshold_s`（默认 5 s）时
   - 缺口内**不做任何积分**；
   - 缺口后的第一个样本打 `gap_before` 标记，记录 `gap` 事件；
   - 按未知电流界 `i_unknown_bound_a`（0.5 A）随缺口时长增加不确定度。
3. **可信静止窗口 + OCV 校准**：连续满足
   - `|I| ≤ 0.05 A` 持续 ≥ 60 s；
   - 电压稳定（相邻阶跃 ≤ 5 mV，窗口内极差 ≤ 20 mV）；
   - 温度在表内（表外温度会使窗口失效）；
   
   则按 `V_ref = V_meas - dVdT·(T - 25°C)`（`dVdT = -0.0002 V/K`）校正到 25°C 参考，
   查冻结 OCV 表（线性插值）得到 SOC，并把不确定度重置为 `sigma_ocv = 0.01`。
4. **不确定度模型**（保守分解，逐样本输出三个分量）：
   - 随机游走：`var += σ_rw²·dt/3600`（每 √小时 0.02）；
   - 电流偏置：`σ_bias += i_bias_bound·dt/(3600·Q_eff)`（偏置界 0.02 A，随时间线性累计）；
   - 缺口未知：`σ_unknown += i_unknown_bound·dt_gap/(3600·Q_eff)`；
   - 汇总：`σ = √var + σ_bias + σ_unknown`；OCV 校准重置全部三个分量。
5. **SOC 钳位**：任何情况下 SOC 都被限制在 [0, 1]，钳位时打
   `soc_clamped_low` / `soc_clamped_high` 标记并记录事件。

## 3. 参数版本冻结与密码学完整性（真实执行）

- `config/params.json` 是冻结参数清单（版本 `soc-estimator-params-v1.0.0`）；
- `python scripts/freeze_params.py` 用 HMAC-SHA256 对清单**规范化字节**
  （排序键、紧凑分隔符、ASCII）签名，输出 `config/params.sig` 与 `params.sha256`；
- 服务**启动时强制验签**（`hmac.compare_digest`），签名不符直接拒绝启动
  （`ParamIntegrityError`），不存在"静默使用未签名参数"路径；
- 密钥来自环境变量 `BTE_PARAM_KEY`（hex）。为免安装可开箱运行，仓库附带
  `config/param_key.dev.hex`，文件中已注明 **DEV ONLY**；生产环境请用环境变量注入独立密钥。

## 4. 迟到数据与重放窗口（真实执行）

- 重放窗口冻结为 `replay_horizon_s = 3600 s`；
- 比 `t_max_stored - 3600 s` 更早的样本判定为**过期**：整批全过期 → `409 STALE_DATA`
  并给出过期时间戳；部分过期 → 接收窗口内样本，响应中如实报告 `stale_rejected`；
- 窗口内迟到样本合并后**从会话锚点（初始 SOC + 全部历史样本）整体确定性重算**，
  绝不做增量污染；重算后重载入盘状态做 **anchor 校验**（重算结果与落盘结果逐位相等）；
- 重复时间戳忽略并计数（`duplicates`）。

## 5. 计算证据（hash 链，真实 SHA-256）

- 每个会话目录：`state.json`（原子写入的会话状态/样本/结果）+ `evidence.jsonl`
  （追加式证据链）；
- 证据链每条记录含 `seq`、`prev_hash`、`hash = SHA256(canonical(record\hash))`；
  篡改、删除、重排任意一行都会使 `GET /v1/sessions/{id}/evidence` 的
  `chain_valid` 变为 `false`（有专门测试）。

## 6. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 + 参数版本/摘要/验签状态 |
| GET | `/v1/params` | 冻结清单全文 + sha256 + HMAC 签名 |
| POST | `/v1/sessions` | 创建会话（可带 `initial_soc`/`initial_sigma`） |
| GET | `/v1/sessions` | 会话列表 |
| GET | `/v1/sessions/{id}` | 会话元信息 + 汇总 + anchor 校验 |
| DELETE | `/v1/sessions/{id}` | 删除会话 |
| POST | `/v1/sessions/{id}/samples` | 遥测摄入（重放窗口/去重/重算） |
| GET | `/v1/sessions/{id}/soc` | 最新 SOC + 不确定度分解 + 末样本标记 |
| GET | `/v1/sessions/{id}/trace?offset&limit` | 逐样本计算轨迹（分页） |
| GET | `/v1/sessions/{id}/events` | 缺口/钳位/OCV 校准等事件 |
| GET | `/v1/sessions/{id}/evidence` | 证据链记录 + `chain_valid` |
| POST | `/v1/sessions/{id}/recompute` | 手动从锚点重算并校验 |
| POST | `/v1/estimate` | 无状态一次性离线估算 |

交互式文档：服务启动后访问 `http://127.0.0.1:8000/docs`。

## 7. 本地启动

```bash
cd b
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt          # 或 pip install -r requirements-lock.txt
python scripts/freeze_params.py              # 已随仓库冻结；更换参数后必须重签
pytest -q                                    # 47 个测试
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

环境变量：`BTE_PARAM_DIR`（默认 `config`）、`BTE_PARAM_KEY`（hex HMAC 密钥）、
`BTE_DATA_DIR`（会话持久化目录，默认 `data`）。

## 8. 验收命令（Acceptance）

```bash
# 1) 全部自动化测试（充放电切换、偏置累计、表外温度、断流、OCV 校准、
#    钳位、迟到/过期重放、重启、参数验签、证据链、HTTP 校验）
pytest -q

# 2) 端到端 demo（先启动 uvicorn，再开一个终端）
./scripts/demo.sh http://127.0.0.1:8000
```

demo 会依次演示：健康检查 → 参数验签 → 建会话 → 摄入示例遥测
（静止校准 → 放电 → **180 s 断流** → 放电 → **-30°C 表外温度** → 充电 → 静止校准）
→ 读取 SOC → 迟到数据部分接收/过期拒绝 → 409 过期拒绝 → 证据链校验。

示例数据由 `python scripts/generate_sample_data.py` 确定性生成（无随机数），
已提交在 `examples/`。

## 9. 目录结构

```
config/
  params.json            # 冻结参数清单（版本号 + 明确单位）
  params.sig             # HMAC-SHA256 签名
  params.sha256          # 清单 SHA-256
  param_key.dev.hex      # 开发密钥（DEV ONLY，生产用 BTE_PARAM_KEY）
app/
  util.py                # 规范化序列化 / SHA-256
  params.py              # 清单加载、HMAC 验签、类型化参数
  engine.py              # 库仑积分 + OCV 校准 + 不确定度（纯函数、确定性）
  evidence.py            # 追加式 SHA-256 证据链
  state.py               # 会话存储、重放窗口、anchor 校验、持久化
  schemas.py             # pydantic 模型（拒绝 NaN/Inf/越界）
  main.py                # FastAPI 应用工厂（验签失败拒绝启动）
scripts/
  freeze_params.py       # 参数冻结/签名
  generate_sample_data.py# 确定性生成示例数据
  demo.sh                # 端到端验收脚本
tests/                   # 47 个 pytest 测试（见 tests/conftest.py）
examples/                # 示例输入（建会话/遥测/迟到数据）
requirements.txt         # 直接依赖，精确钉版
requirements-lock.txt    # 全量传递依赖锁定（venv 内 pip freeze 生成并验证）
```
