# 相机标定版本服务（Camera Calibration Version Service）

纯后端离线相机标定服务：读取本地棋盘格图片，用 **OpenCV 真实算法**检测角点并计算
相机内参与畸变系数；标定结果按**相机 + 分辨率**绑定为**不可变版本**，新旧版本可
并行查询；逐张报告重投影误差，样本剔除规则与姿态覆盖判定全部留痕、可追溯。

- 技术栈：Python 3.12 · OpenCV（`opencv-python-headless` 4.10）· FastAPI · NumPy
- 无前端页面，仅提供 HTTP/JSON 接口与测试
- **不使用任何固定/预置内参矩阵替代计算**：内参与畸变均来自 `cv2.calibrateCamera`
  对上传图像的实际优化；结果用 **HMAC-SHA256 真实签名**，可随时复算防篡改

---

## 1. 目录结构

```
app/
  config.py        # 阈值与路径配置（环境变量可覆盖）
  crypto.py        # SHA-256 内容指纹 + HMAC-SHA256 签名（真实密码学）
  calibration.py   # 角点检测、PnP 位姿、重复视角聚类、覆盖评估、calibrateCamera
  pipeline.py      # 完整流水线与可追溯剔除记录、版本记录组装
  storage.py       # 不可变版本记录的原子持久化与相机/分辨率索引
  synth.py         # 合成投影夹具（真实针孔模型渲染，供示例与测试）
  main.py          # FastAPI 路由
scripts/
  generate_examples.py  # 生成 examples/ 示例输入
tests/             # 验收测试（合成夹具 + 真实算法）
examples/          # 由脚本生成的示例图片（合格/重复视角/异常图）
requirements.txt   # 直接依赖
requirements.lock  # 已验证的精确锁定版本（含传递依赖）
```

## 2. 本地启动

```bash
# 1) 建立虚拟环境并安装锁定依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock      # 或 pip install -r requirements.txt

# 2)（可选）生成示例棋盘图片
python scripts/generate_examples.py

# 3) 启动服务
CALIB_STORAGE_DIR=./data uvicorn app.main:app --host 0.0.0.0 --port 8000
```

健康检查：`GET http://localhost:8000/health`

### 关键环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `CALIB_STORAGE_DIR` | `./data` | 版本记录、输入样本留档、HMAC 密钥目录 |
| `CALIB_HMAC_SECRET` | 自动生成 | HMAC 签名密钥；不提供则在存储目录生成 `0600` 权限随机密钥 |
| `CALIB_MIN_VIEWS` | `5` | 通过标定所需的最少**去重后**视角数 |
| `CALIB_DUP_ROT_DEG` / `CALIB_DUP_TRANS_REL` / `CALIB_DUP_CENTER_REL` | 3 / 0.08 / 0.04 | 重复视角判定阈值（三项同时满足才算重复） |
| `CALIB_MIN_MAX_ANGLE_DEG` / `CALIB_MIN_MEAN_ANGLE_DEG` | 12 / 4 | 姿态覆盖退化的旋转角阈值 |
| `CALIB_MIN_FOV_X` / `CALIB_MIN_FOV_Y` | 0.25 / 0.20 | 角点对画面横/纵向覆盖比例下限 |
| `CALIB_OUTLIER_FLOOR_PX` / `CALIB_OUTLIER_RATIO` | 0.8 / 3.0 | 离群剔除绝对下限与相对中位数倍数 |

## 3. API 协议

### `POST /api/v1/calibrations`  （multipart/form-data）

| 字段 | 类型 | 说明 |
|---|---|---|
| `camera_id` | string | 相机标识 |
| `pattern_cols` / `pattern_rows` | int | 棋盘**内角点**列/行数（如 9×6） |
| `square_size_mm` | float | 棋盘格边长（毫米） |
| `min_views` | int，可选 | 本次要求的最少去重视图数 |
| `images` | 多文件 | 同一分辨率的棋盘图片 |

**成功 201**：返回完整版本记录（内参矩阵、畸变、逐图 RMS、覆盖指标、接受/剔除
清单、逐轮剔除历史、阈值快照、输入 SHA-256、HMAC 签名）。

**失败 422**：统一为 `{"error": {"code", "reason", ...证据字段}}`，常见 code：

| code | 触发条件 |
|---|---|
| `INSUFFICIENT_SAMPLES` | 提交/角点成功图片少于硬性下限（3） |
| `INSUFFICIENT_DISTINCT_VIEWS` | 去重后视角数不足 |
| `POSE_DEGENERACY` | 姿态方向/画面覆盖退化（附 `coverage` 指标） |
| `INSUFFICIENT_VIEWS_AFTER_REJECTION` | 离群剔除后样本不足 |
| `ALL_IMAGES_UNREADABLE` | 没有任何图片可解码 |
| `INVALID_BOARD` | 棋盘参数非法 |

### 版本与内参查询

- `GET /api/v1/cameras/{camera_id}/versions?width=&height=` — 版本列表（可按分辨率过滤）
- `GET /api/v1/cameras/{camera_id}/latest?width=&height=` — 指定精确分辨率的最新版本
- `GET /api/v1/versions/{version_id}` — 按 ID 取任意旧版本（新旧并行、不可变）
- `GET /api/v1/versions/{version_id}/intrinsics?width=&height=` — 取内参；
  请求分辨率与版本绑定分辨率不一致时返回 **409 `RESOLUTION_MISMATCH`**，禁止跨尺寸复用
- `POST /api/v1/versions/{version_id}/verify` — 实时复算 HMAC，检测记录篡改

### curl 示例

```bash
curl -s http://localhost:8000/health

curl -s -X POST http://localhost:8000/api/v1/calibrations \
  -F camera_id=cam_demo_01 \
  -F pattern_cols=9 -F pattern_rows=6 -F square_size_mm=25 \
  -F min_views=5 \
  $(for f in examples/good/1280x960/*.jpg; do printf -- '-F images@%s ' "$f"; done) \
  | python -m json.tool
```

（也可直接 `bash scripts/api_demo.sh` 跑通“标定→查询→取内参→验签”全流程。）

## 4. 处理流水线与可追溯规则

```
解码 ── 分辨率一致性 ── 角点检测(严格→放宽+亚像素) ── PnP 初始位姿
  └─ 重复视角贪心聚类（旋转/平移/投影中心三项同时成立）
  └─ 姿态覆盖初筛（视角最大/平均夹角 + 角点横纵覆盖比）
  └─ cv2.calibrateCamera（5 参数 plumb-bob 畸变，RMS 收敛 1e-8）
  └─ 逐图重投影 RMSE → 迭代离群剔除
       阈值 = max(0.8px, 3.0 × 当轮中位数)，每轮只剔最差一张，全过程记入 rejection_history
  └─ 覆盖复核 ── 组装记录 ── HMAC-SHA256 签名 ── 原子落盘（不可变）
```

- 每张被剔除图片都带：`stage`（decode/resolution/corner/duplicate/outlier）、
  人可读 `reason`、`rms_px`（若有）与数值 `detail`。
- 角点失败时扫描声明尺寸 ±3（含转置）并报告实际能检测到的尺寸，帮助定位
  “棋盘参数填错”问题。
- 输入图片原始字节按版本留档于 `data/images/<version_id>/`（`0600`），
  记录内含每文件 SHA-256，样本可复核。

## 5. 验收（自动化测试）

一键完成“建虚拟环境 → 装锁定依赖 → 生成示例 → 跑全部测试”：

```bash
bash scripts/acceptance.sh
```

或在已有环境中分步执行：

```bash
source .venv/bin/activate
python scripts/generate_examples.py   # 生成 examples/ 示例输入
python -m pytest                      # 全量验收（合成投影夹具 + 真实 OpenCV）
```

测试覆盖：

1. **真实标定质量**：合成相机真值（对标定器不可见）下 fx/fy 恢复误差 <1%、
   主点居中；不同 FOV 数据得到不同内参（证明非固定矩阵）；逐图 RMS 齐全；
   HMAC 验签与篡改检出。
2. **异常图**：损坏文件/纯噪声/无棋盘图被逐张标注且不影响合格图；全部损坏时 422。
3. **重复视角**：近似视角被聚类剔除，记录代表样本；数量不足时 422。
4. **姿态退化**：视角集中时以 `POSE_DEGENERACY` 拒绝并返回覆盖指标。
5. **错误棋盘尺寸**：声明 12×9（实际 9×6）失败，并在诊断中报告可检出的 9×6。
6. **离群剔除**：模糊噪声图被真实剔除，逐轮阈值/中位数/剩余数量可追溯。
7. **版本绑定与并行**：新旧版本共存查询、内参跨分辨率 409、不同相机/分辨率隔离。
8. **不可变性与密码学**：版本不可二次写入；篡改落盘记录后验签失败。

> 说明：合成夹具经超采样渲染并加噪，角点定位本身有 ~0.2px 物理性残差（类似真实
> 光学 PSF），因此逐图 RMS 在亚像素级即代表通过；测试对内参精度而非噪声水平做断言。
