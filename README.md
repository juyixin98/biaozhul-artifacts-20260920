# Camera Calibration Service

纯后端离线相机标定服务：上传棋盘格图片 → OpenCV 检测角点并做亚像素细化 →
`cv2.calibrateCamera` **真实求解**相机内参与畸变系数。标定版本按
「相机 + 分辨率」绑定，绝不跨尺寸复用；逐张报告重投影误差；剔除规则与
样本选择在任务报告中全程可追溯；样本不足、姿态覆盖退化或角点失败时返回明确原因。

## 技术栈

- Python 3.12、FastAPI + Uvicorn、NumPy
- OpenCV 4.14（`findChessboardCorners` / `cornerSubPix` / `calibrateCamera` /
  `projectPoints`，全部真实计算，无任何固定或占位矩阵）
- HMAC-SHA256（hmac/hashlib，密钥来自环境变量或数据目录中 0600 权限的随机密钥）

## 目录结构

```
app/
  config.py       # 环境配置与全部质量阈值（可用环境变量覆盖）
  crypto.py       # SHA-256 内容哈希 + 规范 JSON 的 HMAC-SHA256 签名
  models.py       # Pydantic 协议模型
  storage.py      # 版本/任务原子化 JSON 落盘、签名校验
  calibration.py  # 检测、标定、重复视角/离群剔除、姿态覆盖分析（核心）
  main.py         # FastAPI 路由
scripts/
  generate_fixtures.py  # 用已知真值内参投影合成棋盘格夹具
tests/                  # pytest 自动化验收（24 项）
examples/fixtures/      # 运行脚本后生成的示例输入
requirements.txt        # 完整传递依赖锁定
```

## 本地启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 生成示例棋盘格图片（2x 超采样合成投影，自带 ground_truth.json）
python scripts/generate_fixtures.py examples/fixtures

# 启动（数据目录默认 ./data；HMAC 密钥默认自动生成到 data/hmac.key）
CALIB_DATA_DIR=./data uvicorn app.main:app --host 127.0.0.1 --port 8000
```

健康检查：`curl -s http://127.0.0.1:8000/healthz`

## API

### `POST /calibrations`（multipart 上传）

表单字段：`camera_id`、`width`、`height`（绑定分辨率）、
`board_rows`/`board_cols`（**内角点**行列数）、`square_size_mm`、
`files`（一张或多张图片）。

```bash
curl -s -X POST http://127.0.0.1:8000/calibrations \
  -F camera_id=camA -F width=1280 -F height=960 \
  -F board_rows=6 -F board_cols=9 -F square_size_mm=40 \
  $(for f in examples/fixtures/valid_views/*.png; do echo -F files=@$f; done)
```

- 成功：`201` + 完整报告（`status="succeeded"`），同一相机+分辨率下版本号递增，
  旧版本保留。
- 数据质量失败：`422` + 落盘任务报告，`failure_reason` 为
  `CORNER_DETECTION_FAILED` / `INSUFFICIENT_VIEWS` / `DEGENERATE_POSE_COVERAGE`，
  逐张图片给出拒绝原因。
- 请求格式错误（无文件、非法棋盘参数、路径穿越等）：`422`/`403`。

### `POST /calibrations/from-paths`（读取服务器本地图片）

```json
{
  "camera_id": "camA", "width": 1280, "height": 960,
  "board_rows": 6, "board_cols": 9, "square_size_mm": 40,
  "image_paths": ["examples/fixtures/valid_views/view_00.png"]
}
```

路径必须位于允许根目录内（默认当前工作目录，可用 `CALIB_IMAGE_ROOTS` 追加，
`:` 分隔）；目录穿越返回 `403`。

### 查询（新旧版本并行）

```bash
# 某相机/分辨率的全部版本
curl -s "http://127.0.0.1:8000/cameras/camA/calibrations?width=1280&height=960"
# 最新 / 指定版本（版本详情含逐图重投影误差与位姿）
curl -s "http://127.0.0.1:8000/cameras/camA/calibrations/latest?width=1280&height=960"
curl -s "http://127.0.0.1:8000/cameras/camA/calibrations/v1?width=1280&height=960"
# 失败或成功的任务报告
curl -s http://127.0.0.1:8000/jobs/<job_id>
```

版本严格绑定分辨率：未知分辨率返回 `409` 与该相机下可用分辨率列表，
已知分辨率但版本号不存在返回 `404`，标定结果不会跨尺寸复用。
版本 JSON 带 HMAC-SHA256 `signature`（`sha256=<hex>`）；落盘文件被篡改时
GET 返回 `signature_valid: false`。

## 验收命令

```bash
pip install -r requirements.txt
pytest -q
```

夹具用**已知真值内参**把棋盘真实投影成图；测试用**真实标定算法**求解并与真值
比较（焦距误差 <1%、主点 <4px、k1/切向项均在容差内），覆盖：

- 真值恢复（24 个广覆盖视角，含画面边缘偏置视角）
- 异常图（不可解码文件、无棋盘照片、分辨率不符）
- 重复视角（6 个近似副本，记录 `duplicate_of` 与判定数值）
- 重投影离群剔除（合成局部扭曲图：角点全检出但与单针孔模型不自洽）
- 错误棋盘尺寸（7x10 vs 实际 6x9 内角点）
- 样本不足（<12 张）
- 姿态覆盖退化（共线平移，SVD 比值 ≈ 0）
- 新旧版本并行查询、分辨率绑定（409/404 语义）
- 签名防篡改、路径穿越、HMAC/SHA-256 已知向量

## 可追溯的剔除流水线

每张图片在报告 `images[]` 中有独立条目：`sha256`、像素尺寸、`status`、
`reason`、`detail`（判定时的具体数值）、`reprojection_error_px`、
`pose.rvec/tvec`，重复视角另有 `duplicate_of`。

1. 解码 → 分辨率校验（`decode_failed` / `resolution_mismatch`）；
2. `findChessboardCorners` + `cornerSubPix`（`corner_detection_failed`）；
3. 首轮标定后做**重复视角**检测：相对旋转角 < `CALIB_DUP_ANGLE_DEG`（默认 2°）
   且相机中心间距 < 视角中心间距中位数 × `CALIB_DUP_TRANS_RATIO`（默认 0.08）
   的后到视角判重；
4. 重新标定并迭代剔除重投影 RMSE
   > `max(CALIB_OUTLIER_MAX_PX, CALIB_OUTLIER_MEDIAN_K × 中位数误差)`
   （默认 1px / 3 倍中位数）的视角，最多 5 轮；被剔视图保留其测得的误差；
5. 通过视角不足 `CALIB_MIN_VIEWS`（默认 12）→ `INSUFFICIENT_VIEWS`；
6. **姿态覆盖退化**：相机中心 SVD 最小/最大奇异值比 <
   `CALIB_COVERAGE_SVD_RATIO`（默认 0.05，视点共线/共面），
   或棋盘法向倾角展宽 < `CALIB_COVERAGE_TILT_DEG`（5°）且视向锥展宽
   < `CALIB_COVERAGE_RAY_DEG`（8°）→ `DEGENERATE_POSE_COVERAGE`。

畸变模型为标准 5 系数向量 (k1, k2, p1, p2, k3)，固定 k3=0：k3 只有在角点
覆盖极端画面边缘时才可辨识，放开会在不损失重投影误差的情况下把 k1/k2
拖成无意义的值。`exclusions` 汇总各类剔除计数，`thresholds` 回显本次任务
实际生效的阈值。
