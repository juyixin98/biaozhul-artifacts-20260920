#!/usr/bin/env bash
# 从项目根目录运行全部请求样例，并打印响应。
# 用法：bash examples/run_all.sh
set -u
cd "$(dirname "$0")/.."

echo "############ distance: 跨反经线 (0,179)-(0,-179) ############"
./sphdist examples/req_distance_antimeridian.json
echo
echo "############ distance: 北极重合（经度不同）############"
./sphdist examples/req_distance_pole_coincident.json
echo
echo "############ distance: 对跖点（上海附近↔阿根廷附近）############"
./sphdist examples/req_distance_antipodal.json
echo
echo "############ distance: 北京-上海 ############"
./sphdist examples/req_distance_beijing_shanghai.json
echo
echo "############ range: 反经线 180° 上 150 km ############"
./sphdist examples/req_range_antimeridian.json
echo
echo "############ range: 北极点 300 km（经度任意）############"
./sphdist examples/req_range_north_pole.json
echo
echo "############ range: 东京 3000 km（内联点集）############"
./sphdist examples/req_range_inline_points.json
echo
echo "############ error: 纬度越界（退出码应为 1）############"
./sphdist examples/req_error_lat_out_of_range.json
echo "exit code: $?"
