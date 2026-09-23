# tsblock HTTP 请求样例
#
# 所有样例假设服务运行在 127.0.0.1:8080：
#   ./target/release/tsblock --addr 127.0.0.1:8080 --data-dir ./data --block-size 1000 --fault
#
# 写入点的请求体是每行一个 `timestamp,value`（均为有符号 i64），空行忽略。
# 响应均为手工生成的 JSON。完整可运行脚本见 requests.sh。

## 健康检查
curl -s http://127.0.0.1:8080/health

## 列出所有序列
curl -s http://127.0.0.1:8080/series

## 创建序列（policy=reject 默认；block_size 覆盖全局默认）
curl -s -X POST "http://127.0.0.1:8080/series/cpu?policy=reject&block_size=1000"

## 创建“乱序缓冲”策略的序列
curl -s -X POST "http://127.0.0.1:8080/series/late?policy=buffer"

## 写入点（值含负数）
curl -s -X POST http://127.0.0.1:8080/series/cpu/points \
  --data-binary $'1700000000,-5\n1700000060,-3\n1700000120,-4\n1700000180,-1'

## 写入并在响应前强制落盘（flush + fsync）
curl -s -X POST "http://127.0.0.1:8080/series/cpu/points?sync=true" \
  --data-binary $'1700000240,2\n1700000300,5'

## 边界查询（闭区间 [start,end]）；时间和值都允许 i64 极值
curl -s "http://127.0.0.1:8080/series/cpu/range?start=1700000060&end=1700000240"
curl -s "http://127.0.0.1:8080/series/cpu/range?start=-9223372036854775808&end=9223372036854775807"

## 压缩统计（真实压缩比 = 未压缩 16B/点 ÷ 实际磁盘字节）
curl -s http://127.0.0.1:8080/series/cpu/stats

## 手动把活动块 fsync 成一个数据块
curl -s -X POST http://127.0.0.1:8080/series/cpu/flush

## 乱序点（reject 策略 -> 整批拒绝，HTTP 409，已有点不变）
curl -s -w "\nHTTP %{http_code}\n" -X POST http://127.0.0.1:8080/series/cpu/points \
  --data-binary $'1700000360,9\n100,0'

## buffer 策略：迟到点进入侧缓冲，查询不可见
curl -s -X POST http://127.0.0.1:8080/series/late/points --data-binary $'100,1\n200,2\n300,3'
curl -s -X POST http://127.0.0.1:8080/series/late/points --data-binary $'150,9\n50,0'
curl -s http://127.0.0.1:8080/series/late/buffered
curl -s "http://127.0.0.1:8080/series/late/range?start=-9223372036854775808&end=9223372036854775807"

## drain：压缩重写，把侧缓冲并入历史（原子 rename + 目录 fsync）
curl -s -X POST http://127.0.0.1:8080/series/late/drain
curl -s "http://127.0.0.1:8080/series/late/range?start=-9223372036854775808&end=9223372036854775807"

## 故障注入（仅 --fault 启动时可用）
# 再写入 N 字节后让写操作返回 ENOSPC（跨界写只落前半段 -> 产生尾部残记录）
curl -s -X POST http://127.0.0.1:8080/dev/fault --data-binary $'write_limit=64'
# 让下一次 fsync 返回 EIO
curl -s -X POST http://127.0.0.1:8080/dev/fault --data-binary $'fail_syncs=1'
# 清除全部注入故障
curl -s -X POST http://127.0.0.1:8080/dev/fault --data-binary $'clear'
