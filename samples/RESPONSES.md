# 示例请求 / 响应（实测存档）

以下输出来自 `./build/rect_union --file samples/<name>.json`，与
`make check` 中的断言一致。键顺序以程序实际输出为准（JSON 对象无序）。

## 1) overlap.json —— 部分重叠的三个正方形

请求：

```json
{
  "rectangles": [
    {"x1": 0, "y1": 0, "x2": 2, "y2": 2, "id": "A"},
    {"x1": 1, "y1": 1, "x2": 3, "y2": 3, "id": "B"},
    {"x1": 3, "y1": 0, "x2": 5, "y2": 2, "id": "C"}
  ]
}
```

响应（实测）：

```json
{
  "ok": true,
  "area": 11,
  "perimeter": 18,
  "rectangles_in": 3,
  "rectangles_used": 3,
  "ignored_rectangles": [],
  "units": {
    "coordinate_system": "cartesian, y-up, integer lattice",
    "area_unit": "square grid units",
    "length_unit": "grid units",
    "semantics": "half-open [x1,x2) x [y1,y2)"
  }
}
```

核验：A、B 各 4，重叠 1；C 为 4 且与 B 仅点接触于 (3,2)（半开边界不重合），
面积 4+4−1+4 = 11。

## 2) adjacent_and_degenerate.json —— 相邻 + 三种退化盒

两个沿 x=2 相邻的 2×2：缝不计周长，结果 2×4 矩形 area=8、perimeter=12。
竖直线段、水平线段、点三个退化盒均被跳过并报告：

```json
{
  "ok": true,
  "area": 8,
  "perimeter": 12,
  "rectangles_in": 5,
  "rectangles_used": 2,
  "ignored_rectangles": [
    {"index": 2, "id": "degenerate-horizontal-edge",
     "reason": "zero-area rectangle has empty half-open interior"},
    {"index": 3, "id": "degenerate-vertical-edge",
     "reason": "zero-area rectangle has empty half-open interior"},
    {"index": 4, "id": "degenerate-point",
     "reason": "zero-area rectangle has empty half-open interior"}
  ]
}
```

## 3) nested.json —— 三层嵌套与相邻

- 外框 `[0,10)×[0,10)`，middle、inner 完全内嵌（不增加面积/周长）；
- left-neighbor 为 `[−5,0)×[−5,10)`（5×15 = 75），与外框沿 x=0 相邻，
  但 y 方向多出 [−5,0) 一段，故合并后：

```json
{"ok": true, "area": 175, "perimeter": 60, "rectangles_in": 4,
 "rectangles_used": 4, "ignored_rectangles": []}
```

核验：面积 = 100 + 75 = 175。并集是两个矩形沿 x=0 拼接、左件向下多伸出
5 格的 L 形。按事件截面的对称差计数：竖直方向暴露量 15（x=−5）+ 5（x=0
处只多出 [−5,0) 那一截）+ 10（x=10）= 30；水平方向 5（y=−5）+ 10（y=0
处右件新加入）+ 15（y=10）= 30；周长 60。该数值也由独立的逐格参考
`gridReference()` 对拍验证（见 `make check`）。

## 4) same_x_events.json —— 同一 x 多事件组

5 个矩形组成的楼梯形，多个开始/结束事件共享同一 x（含同坐标既开始又结束）：

```json
{"ok": true, "area": 16, "perimeter": 18, "rectangles_in": 5,
 "rectangles_used": 5, "ignored_rectangles": []}
```

## 5) invalid_fraction.json —— 非法（小数坐标）

```json
{"rectangles": [{"x1": 0.5, "y1": 0, "x2": 2, "y2": 2}]}
```

```
{"ok": false, "error": {"code": "INVALID_RECTANGLE",
 "message": "rectangle at index 0: field \"x1\" must be an integer (fractions not allowed)"}}
```

CLI 退出码为 1，HTTP 状态码为 400。

## 6) HTTP 实测（端口以实际为准）

```
$ curl -s http://127.0.0.1:39147/healthz
{"ok":true,"status":"healthy"}

$ curl -s -X POST http://127.0.0.1:39147/union \
    -H 'Content-Type: application/json' \
    -d '{"rectangles":[{"x1":0,"y1":0,"x2":3,"y2":3},
                       {"x1":3,"y1":0,"x2":6,"y2":3}]}'
{"ok":true,"area":18,"perimeter":18,...}
```
