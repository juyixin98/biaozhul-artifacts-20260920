# URDF 惯性检查服务（纯后端）

离线检查 URDF 机器人模型的**结构引用**与**物理（惯性）参数**。
Python + lxml + NumPy 实现，无任何前端、不连接/控制硬件、不发起网络请求。

- 结构：link/joint 重名、parent/child 引用、未知关节类型；运动链必须是
  **单棵有根树**（允许 `fixed` 关节，但循环、自环、多根森林一律拒绝）。
- 关节：轴必须是有限三维向量且**单位归一化**（浮点容差可配）；
  `revolute`/`prismatic`/`continuous` 必须有 `<limit>`，校验
  `effort`/`velocity` 数值，以及 `lower <= upper`。
- 惯性：每个 link 必须有 `<inertial>`；**「缺少」与「数值非法」是不同错误码**；
  质量必须为正（零质量单独报 `MASS_ZERO`）；惯量矩阵用 NumPy 求特征值，
  判定**对称正定**（容差内的半正定边界只告警），并对主转动惯量验证
  **三角不等式** I₃ ≤ I₁ + I₂。
- 单位/量级：由质量与惯量反推等效回转半径，超出合理机械尺寸范围只告警
  （常见于 kg/g、m/mm 未换算）。
- 安全：**禁止 XML 外部实体（XXE）与一切 DOCTYPE/ENTITY**；**不执行 xacro
  宏**（检测到 `xacro:` 命名空间、`<xacro:*>` 或 `${...}` 直接拒绝，
  要求先离线展开）。解析器同时关闭 DTD 加载、实体解析与网络访问。

## 目录结构

```
urdf_check/
  issues.py        # 问题模型与全部错误码（缺少 / 数值非法 / 物理非法分开）
  parser.py        # 安全 XML 解析：DOCTYPE/ENTITY/xacro 预扫描 + 加固 lxml
  checker.py       # 结构树、关节轴/限位、惯性矩阵（NumPy 特征值）检查
  cli.py           # 命令行入口（文本 / JSON）
examples/
  fixture_a_single_link.urdf        # 夹具 A：最小单连杆
  fixture_b_chain.urdf              # 夹具 B：5 link / 3 DOF 链（含 fixed）
  boundary_triangle_equal.urdf      # 边界矩阵 1：I3=I1+I2 等号，合法
  boundary_triangle_violation.urdf  # 边界矩阵 2：正定但违反三角不等式，非法
  badcase_demo.urdf                 # 各类错误的综合反例
tests/             # 76 个 pytest 用例
requirements.txt   # 运行依赖（精确版本）
requirements.lock  # 全量锁定（含开发依赖与传递依赖）
```

## 本地启动

需要 Python 3.10+。

```bash
cd /home/admin/Downloads/biaozhul/P047/a

# 1) 建虚拟环境并安装锁定依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # 或 -r requirements.lock

# 2) 文本报告
.venv/bin/python -m urdf_check examples/fixture_b_chain.urdf

# 3) 机器可读 JSON
.venv/bin/python -m urdf_check examples/boundary_triangle_violation.urdf --json
```

退出码：`0` 通过（可能有 warning）；`1` 存在 error；
加 `--strict-warnings` 后 warning 也令退出码为 1。
`--axis-tol` 可覆盖轴归一化容差（默认 1e-6）。

### 作为库使用

```python
from urdf_check import inspect_urdf_file

report = inspect_urdf_file("robot.urdf")
print(report.ok)                 # bool：无 error 即为 True
for issue in report.issues:
    print(issue.severity, issue.code, issue.node, issue.attribute, issue.line)
# report.to_dict() -> JSON 可序列化结构
```

每条问题都带定位：`node`（形如
`/robot[@name='r']/link[@name='arm']/inertial/inertia`）、
`attribute`（如 `ixx`、`value`、`xyz`）、`line`（源行号）。

## 验收命令

```bash
# 1) 自动化测试（76 用例：零质量、重名 link、浮点容差、单位量级、XXE、循环…）
.venv/bin/python -m pytest -q

# 2) 两套最小夹具 + 合法边界矩阵：必须全部 exit 0
for f in examples/fixture_a_single_link.urdf \
         examples/fixture_b_chain.urdf \
         examples/boundary_triangle_equal.urdf; do
  .venv/bin/python -m urdf_check "$f" || echo "FAIL: $f"
done

# 3) 非法边界矩阵：必须 exit 1 且报 INERTIA_TRIANGLE_VIOLATION
.venv/bin/python -m urdf_check examples/boundary_triangle_violation.urdf

# 4) 安全验收：XXE 实体文件内容不得出现在输出中，且 exit 1
printf '<?xml version="1.0"?>\n<!DOCTYPE r [<!ENTITY s SYSTEM "file:///etc/hostname">]>\n<robot name="&s;"/>' \
  > /tmp/xxe.urdf
.venv/bin/python -m urdf_check /tmp/xxe.urdf --json
```

## 判定标准与容差

| 项目 | 判定 | 失败级别 |
|---|---|---|
| 缺 `<inertial>` / `<mass>` / `value` / `<inertia>` / 分量属性 | 缺少类错误码 | error |
| 质量/惯量为 `NaN`、`Inf`、不可解析 | `*_INVALID_NUMERIC` | error |
| 质量 = 0 / < 0 | `MASS_ZERO` / `MASS_NEGATIVE` | error |
| 最小特征值 < −tol | `INERTIA_NOT_POSITIVE_DEFINITE` | error |
| 主惯量 I₃ − I₁ − I₂ > tol | `INERTIA_TRIANGLE_VIOLATION` | error |
| 最小主惯量在 0 附近（相对 < 1e-6） | `INERTIA_NEAR_SEMIDEFINITE` | warning |
| 等效回转半径 < 1e-4 m 或 > 1e2 m | `INERTIA_SCALE_ANOMALY` | warning |

正定与三角不等式都在**主转动惯量（特征值）**上判定，因此非对角元
（积惯量）较大的旋转惯量也能被正确识别。默认容差（可在 `inspect_*`
的 `tol=` 参数中覆盖）：轴归一化 `1e-6`、特征值相对容差 `1e-9`、
三角不等式相对容差 `1e-9`。

## 安全说明

- 解析前对原始字节做预扫描，命中 `<!DOCTYPE>` / `<!ENTITY>` / xacro
  模板即直接拒绝，**不把该文件交给实体解析器**，从源头杜绝内部实体
  扩展（billion laughs）与外部实体读取。
- lxml parser 额外设置 `load_dtd=False`、`resolve_entities=False`、
  `no_network=True`、`huge_tree=False` 作为纵深防御。
- 本服务纯只读：不写文件、不加载插件、不执行宏、不连接任何控制器。
