# laser-deskew：离线二维激光扫描运动去畸变服务

输入每点采样时刻与机体位姿序列，将一帧扫描内的所有点统一到指定参考时刻，
消除传感器运动造成的畸变。仅使用合成数据 / 离线回放，不接真实硬件，不做可视化。

## 坐标系与变换约定

- **W（world/odom）**：世界/里程计坐标系，一帧扫描内固定。
- **B(t)（body）**：机体坐标系。位姿序列给出 `T_W_B(t) = (x, y, theta)`，
  即机体系在世界系中的位姿（把机体系坐标映射到世界系）。
- **L（laser）**：激光坐标系，固连于机体。外参 `T_B_L = (ex, ey, etheta)`
  为激光系在机体系中的恒定位姿（把激光系坐标映射到机体系）。

`t_i` 时刻在激光系测得点 `p_L`（极坐标 `range, angle`），其世界坐标为：

```
p_W = T_W_B(t_i) @ T_B_L @ p_L
```

去畸变把每个点重表达到参考时刻 `t_ref` 的激光系中：

```
p_L_ref = inv(T_W_B(t_ref) @ T_B_L) @ T_W_B(t_i) @ T_B_L @ p_L
```

位姿插值：平移线性插值，偏航角用 SciPy `Slerp` 球面插值（正确处理 ±π 环绕）。
**不做外推**：位姿序列必须覆盖所有点采样时刻与参考时刻，否则明确拒绝
（库层抛 `PoseCoverageError`，API 返回 HTTP 400）。

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 启动服务

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- `GET /health` → `{"status": "ok"}`
- `POST /deskew`：请求体见 `app/schemas.py`（`ranges`/`angles`/`point_times`
  等长；`poses` 为 `{t, x, y, theta}` 列表；`reference_time`；可选 `extrinsic`）。
  返回参考时刻激光系下的 `(x, y)` 点列。位姿覆盖不足返回 400 及原因。

示例（`curl`）：

```bash
curl -sX POST http://127.0.0.1:8000/deskew \
  -H 'Content-Type: application/json' \
  -d '{"ranges":[1.0,1.0],"angles":[0.0,0.1],"point_times":[0.0,0.01],
       "poses":[{"t":-0.01,"x":0,"y":0,"theta":0},{"t":0.02,"x":0.03,"y":0,"theta":0.01}],
       "reference_time":0.0,"extrinsic":{"x":0.1,"y":0.0,"theta":0.0}}'
```

## 运行测试与示例

```bash
.venv/bin/pytest -v                 # 自动化测试（核心数学 + API）
.venv/bin/python examples/run_example.py   # 离线回放示例，打印校正前后残差
```

## 验收逻辑

`app/synthetic.py` 合成匀速平移 + 旋转的机体扫描直墙 `x = wall_x` 的场景，
光线投射求真值距离。对比：

- 未校正：原始距离/角度直接当作参考时刻激光系坐标（忽略运动）；
- 已校正：`deskew_scan` 输出。

两者都经参考时刻真值位姿映回世界系，计算到墙面的 RMSE。由于真值运动
关于时间线性，插值是精确的，校正后残差应降到浮点误差量级（测试阈值 1e-9 m）。
位姿序列覆盖不足（扫描首尾缺位姿、参考时刻超出范围）时明确拒绝。

## 目录结构

```
app/deskew.py      核心 SE(2) 去畸变（NumPy + SciPy Slerp）
app/synthetic.py   合成直墙扫描数据与残差评估
app/schemas.py     API 请求/响应模型
app/main.py        FastAPI 服务
examples/run_example.py  离线回放示例
tests/             pytest 测试
```
