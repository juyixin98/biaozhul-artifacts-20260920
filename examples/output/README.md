# 示例实际输出（验收凭证）

本目录保存的是对 `examples/0*.json` 运行以下命令产生的**真实响应**（未手工编辑）：

```bash
for f in examples/0*.json; do
  ./build/polygon_clip "$f" -o "examples/output/$(basename "$f" .json).out.json"
done
```

结果汇总（退出码 0 = 正常；自交拒绝为 1）：

| 用例 | exit | status | kind | 顶点数 | area |
|---|---|---|---|---|---|
| 01_hand_triangle | 0 | OK | POLYGON | 4 | 50 |
| 02_fully_contained | 0 | OK | POLYGON | 4 | 36 |
| 03_no_intersection | 0 | OK | EMPTY | 0 | 0 |
| 04_edge_coincidence | 0 | OK | POLYGON | 4 | 12 |
| 05_degenerate_segment | 0 | OK | SEGMENT | 2 | 0 |
| 06_degenerate_point | 0 | OK | POINT | 1 | 0 |
| 07_concave_subject | 0 | OK | POLYGON | 4 | 14.011895750144692 |
| 08_self_intersect_rejected | 1 | SELF_INTERSECTING | EMPTY | 0 | 0 |

这些文件可通过重新运行上面的命令随时复现。
