# URDF 惯性检查(urdf-inertia-check)

URDF 离线结构与物理参数检查服务。纯后端,不控制任何硬件;所有解析、
矩阵计算与协议处理均真实执行,检查失败如实以非零退出码 / 422 报告。

## 功能

**安全解析(`urdf_check/safe_xml.py`)**
- 禁止 XML 外部实体与实体扩展(解析器层 `resolve_entities=False`、
  `no_network=True`、`load_dtd=False`,解析后再拒绝 DOCTYPE 与实体节点,
  双保险防 XXE / billion laughs);
- 禁止执行任意宏:检测到 xacro 命名空间内容即拒绝(`XACRO_FORBIDDEN`),
  绝不展开。

**结构检查(`urdf_check/checks.py`)**
- link/joint 重名;joint 的 parent/child 必须引用已定义 link;
- 运动学树:恰好一个根、全连通、无环(固定关节合法,环不容忍);
- 关节轴:零向量报错,非单位向量告警(未给轴时用 URDF 默认 `1 0 0`);
- 限位:revolute/prismatic 必须有 `<limit>`,`lower <= upper`,
  `effort`/`velocity` 必须为非负有限数。

**惯性检查(NumPy 真实计算)**
- 缺失惯性(`MISSING_INERTIAL`,告警)与数值非法(`INVALID_NUMERIC`,
  错误)严格分开;
- 质量必须为正有限数(零质量、负质量分别报错);
- 惯性矩阵对称性、正定性(`eigvalsh` 特征值,容差可配)、
  三角不等式(主对角元与主惯量双重校验);
- 单位量级异常告警(质量/惯量超出常见量级,提示确认单位)。

**错误定位**:每条诊断携带节点 XPath、属性名与源文件行号。

## 目录结构

```
urdf_check/          # 库与服务代码
  safe_xml.py        # 安全解析(禁实体/禁宏)
  model.py           # URDF 数据模型与数值解析
  checks.py          # 全部检查项与容差配置
  service.py         # 检查编排 check_urdf()
  cli.py             # 命令行入口
  server.py          # HTTP 服务(仅标准库)
examples/            # 两套最小机器人夹具
  two_link_arm.urdf      # 两连杆单旋转关节
  fixed_sensor_rig.urdf  # 固定关节传感器支架
tests/               # 50 个自动化测试(含边界矩阵夹具)
requirements.txt     # 锁定依赖
```

## 本地启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 命令行检查(退出码:0 通过 / 1 有错误 / 2 输入不可解析或不安全)
.venv/bin/python -m urdf_check.cli examples/two_link_arm.urdf
.venv/bin/python -m urdf_check.cli examples/fixed_sensor_rig.urdf --json

# 启动 HTTP 服务
.venv/bin/python -m urdf_check.server --host 127.0.0.1 --port 8080
```

HTTP 接口:
- `GET /healthz` → `{"status": "ok"}`
- `POST /check`,请求体为 URDF XML → 200(通过)或 422(有错误),
  响应为 JSON 报告(含每条诊断的 node/attribute/line)。

```bash
curl -s -X POST --data-binary @examples/two_link_arm.urdf \
     http://127.0.0.1:8080/check | python3 -m json.tool
```

## 验收命令

```bash
# 1. 全部自动化测试(安全解析、结构、惯性、容差、CLI、HTTP)
.venv/bin/python -m pytest tests/ -v

# 2. 两套夹具必须通过
.venv/bin/python -m urdf_check.cli examples/two_link_arm.urdf
.venv/bin/python -m urdf_check.cli examples/fixed_sensor_rig.urdf

# 3. 反例必须失败(退出码非 0)
printf '<robot name="b"><link name="a"><inertial><mass value="0"/>\
<inertia ixx="1" iyy="1" izz="1" ixy="0" ixz="0" iyz="0"/></inertial>\
</link></robot>' > /tmp/bad.urdf
.venv/bin/python -m urdf_check.cli /tmp/bad.urdf   # 退出码 1,MASS_ZERO
```

## 容差配置

CLI 参数:`--axis-norm-tol`、`--psd-atol`、`--triangle-rtol`;
库用法:`check_urdf(source, CheckConfig(psd_atol=1e-6, ...))`。
默认值见 `urdf_check/checks.py` 的 `CheckConfig`。

## 诊断码一览(节选)

| 代码 | 级别 | 含义 |
|---|---|---|
| `DTD_FORBIDDEN` / `ENTITY_FORBIDDEN` | error | 含 DTD/实体,拒绝解析 |
| `XACRO_FORBIDDEN` | error | 含 xacro 宏,不执行 |
| `DUPLICATE_LINK_NAME` / `DUPLICATE_JOINT_NAME` | error | 重名 |
| `JOINT_UNKNOWN_LINK` | error | 引用未定义 link |
| `NO_ROOT_LINK` / `MULTIPLE_ROOTS` / `KINEMATIC_CYCLE` | error | 树结构非法/有环 |
| `AXIS_ZERO` / `AXIS_NOT_NORMALIZED` | error/warning | 轴向量零/未归一化 |
| `MISSING_LIMIT` / `LIMIT_BOUNDS_INVERTED` / `LIMIT_NEGATIVE` | error | 限位问题 |
| `MISSING_INERTIAL` | warning | 缺失惯性(独立类别) |
| `INVALID_NUMERIC` | error | 数值非法(非浮点/NaN/Inf) |
| `MASS_ZERO` / `MASS_NEGATIVE` | error | 质量为零/为负 |
| `INERTIA_NOT_POSITIVE_DEFINITE` | error | 惯性矩阵非正定 |
| `INERTIA_TRIANGLE_VIOLATION` | error | 违反三角不等式 |
| `MASS_MAGNITUDE_UNUSUAL` / `INERTIA_MAGNITUDE_UNUSUAL` | warning | 单位量级异常 |
