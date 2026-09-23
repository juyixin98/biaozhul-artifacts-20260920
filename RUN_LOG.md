# 运行记录（实际执行，非虚构）

环境：Ubuntu 24.04，g++ 13.3.0（-std=c++17），GNU make，Python 3.12.3，x86_64。
无 cmake，构建使用 Makefile；除 g++ 标准库外无第三方依赖。

## 1. 干净构建

命令：

```
rm -rf build tests/out
make
```

实际输出：

```
mkdir -p build
g++ -O2 -Wall -Wextra -Wpedantic -std=c++17 src/main.cpp src/mesh_check.cpp -o build/meshcheck
```

结果：退出码 0，无编译警告、无错误。

（开发过程中曾出现 1 条 GCC 13 对含 std::vector 成员对象拷贝的 -Warray-bounds 误报，
已通过合并边分类循环、改用移动语义消除；最终干净构建零警告。）

## 2. 自动化测试

命令：`make test`（内部执行 `python3 tests/run_tests.py`）

结果：

```
69 passed, 0 failed
```

11 组用例、69 项断言全部通过，退出码 0。用例：封闭四面体（含 V-E+F=2 欧拉示性数）、
开洞扇形曲面、三面共边非流形、重复面（同向/循环置换）、翻转面方向冲突、零面积退化、
重合点合并、越界/错误元数/NaN 请求错误、stdin 与坏 JSON 退出码、--eps-rel 0 关闭重合点合并、两个不相交封闭体分量。

开发期间真实出现并修复的失败（最终均已通过）：
- 测试 06「退化面引用点应判孤立」初次 FAIL：孤立点统计曾把退化面引用的点算作被引用；
  已改为仅统计参与拓扑分析的面（active set）。
- 测试 08c「NaN 坐标」初次崩溃：NaN 首字母为大写 'N'，宽容解析分支误放在小写 'n'；
  已修正为 case 'N'。
- `--eps-rel 0` 抽查时发现并未关闭重合点合并：有效容差原为加法（eps_abs+eps_rel*diag），
  相对项为 0 时绝对地板仍生效；已改为合并阈值取 max 且 eps_rel<=0 直接禁用合并，并补回归测试 09b。

## 3. 验收样例（命令与结果）

命令格式：`./build/meshcheck examples/<name>.json`（响应另存 examples/responses/）。

| 样例 | 退出码 | 关键结果 |
|---|---|---|
| closed_tetrahedron | 0 | ok=true；1 分量，F=4/V=4，closed/manifold/orientable 全 true；边界/非流形/冲突/重复全 0 |
| open_fan_surface | 0 | 1 分量不封闭；9 条边界边；孤立点 [9]；F=7/V=9 |
| three_fans_non_manifold | 1 | 非流形边 **edge=[0,1]，face_ids=[1,2,3]**，分量 0；另有独立三角片为分量 1（共 2 分量）；9 条边界边；孤立点 [8] |
| flipped_face_tetrahedron | 1 | 边入射次数封闭（closed=true、边界 0），但 3 条边方向冲突 [1,2]/[1,3]/[2,3]，face_ids 均涉及翻转面 3；orientable=false |
| degenerate_and_duplicate | 0 | 退化面 [11,12]（共线零面积、重复角点）；重合点合并 [1,4]；仅 1 个有效面进入分析；与拓扑错误分离 |
| minimal_request | 0 | 单个三角形，3 条边界边 |

三面共边的实际报告片段（examples/responses/three_fans_non_manifold.response.json）：

```json
"non_manifold_edges": [
  { "edge": [0, 1], "face_ids": [1, 2, 3], "component": 0 }
]
```

连通分量统计（同文件 components）：

```json
[
  { "component": 0, "face_count": 3, "vertex_count": 5, "boundary_edges": 6, "non_manifold_edges": 1, "closed": false },
  { "component": 1, "face_count": 1, "vertex_count": 3, "boundary_edges": 3, "non_manifold_edges": 0, "closed": false }
]
```

## 4. 边界行为实测

```
echo '{"points":[[0,0,0],[1,0,0],[0,1,0]],"faces":[[0,1,2]]}' | ./build/meshcheck --compact
  -> ok=true 的紧凑 JSON，退出码 0（stdin 正常）
echo '{bad json' | ./build/meshcheck
  -> meshcheck: invalid JSON at offset 1: expected string key in object；退出码 2
./build/meshcheck /nonexistent.json
  -> meshcheck: cannot open ...: No such file or directory；退出码 2
```

## 5. 未通过项 / 遗留

- 无：构建零警告，自动化测试 69/69 通过，三类验收几何（封闭体、开洞、三面共边）输出均符合预期。
- 非缺陷的设计取舍（见 README「已知限制」）：重合点合并为 O(n²)；id 以 JSON number 输出（2^53 内安全）；
  非三角面按请求错误拒绝；开洞/孤立点/退化不把退出码置为 1（属信息项而非错误项）。
