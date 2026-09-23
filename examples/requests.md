# HTTP 请求样例

服务参数：`windowSize=10`、`allowedLateness=2`（窗口左闭右开）。
启动：`./run.sh --port 8080 --window-size 10 --allowed-lateness 2`

下列 `curl` 即自动化测试所用的确定性序列。`globalWatermark` 在没有任何活跃分区水位线时为
`null`（内部表示为 `Long.MIN_VALUE`）。

## 1. 写入事件（单条或数组）

```bash
curl -s -XPOST localhost:8080/api/events \
  -H 'Content-Type: application/json' \
  -d '[
    {"id":"e1","key":"a","eventTime":3,"partition":"p1"},
    {"id":"e2","key":"b","eventTime":7,"partition":"p2"},
    {"id":"e3","key":"a","eventTime":9,"partition":"p1"},
    {"id":"e10","key":"c","eventTime":0,"partition":"p2"}
  ]'
```

事件字段：`id`（字符串，窗口内去重键）、`key`（计数分组）、`eventTime`（整数事件时间）、
`partition`（分区名）。

## 2. 窗口边界：ts=10 属于 [10,20)，不属于 [0,10)

```bash
curl -s -XPOST localhost:8080/api/events \
  -H 'Content-Type: application/json' \
  -d '[
    {"id":"e7","key":"a","eventTime":10,"partition":"p1"},
    {"id":"e8","key":"b","eventTime":19,"partition":"p2"}
  ]'
```

## 3. 显式水位线（全局水位 = 活跃分区最小值）

```bash
# p1=10 后什么也不关闭：p2 仍为最小值（未上报）
curl -s -XPOST localhost:8080/api/watermark \
  -H 'Content-Type: application/json' -d '{"partition":"p1","watermark":10}'

# p2=10：全局水位=10，窗口 [0,10) 立即关闭，count=4，closedAtWatermark=10
curl -s -XPOST localhost:8080/api/watermark \
  -H 'Content-Type: application/json' -d '{"partition":"p2","watermark":10}'
```

## 4. 容忍期内迟到事件触发重发；重复事件丢弃

```bash
curl -s -XPOST localhost:8080/api/events -H 'Content-Type: application/json' \
  -d '{"id":"e4","key":"a","eventTime":5,"partition":"p1"}'   # ACCEPTED（wm=10 < end+2=12）
curl -s -XPOST localhost:8080/api/events -H 'Content-Type: application/json' \
  -d '{"id":"e4","key":"a","eventTime":5,"partition":"p1"}'   # DUPLICATE

curl -s -XPOST localhost:8080/api/watermark -H 'Content-Type: application/json' \
  -d '{"partition":"p1","watermark":11}'
curl -s -XPOST localhost:8080/api/watermark -H 'Content-Type: application/json' \
  -d '{"partition":"p2","watermark":11}'                       # [0,10) 重发，count=5
```

## 5. 容忍期结束 → 清除；之后的迟到事件进侧输出

```bash
curl -s -XPOST localhost:8080/api/watermark -H 'Content-Type: application/json' \
  -d '{"partition":"p1","watermark":12}'
curl -s -XPOST localhost:8080/api/watermark -H 'Content-Type: application/json' \
  -d '{"partition":"p2","watermark":12}'
# 全局水位=12 >= 10+2：[0,10) 被 purge，最后一次发射标记 "final":true

curl -s -XPOST localhost:8080/api/events -H 'Content-Type: application/json' \
  -d '{"id":"e6","key":"a","eventTime":6,"partition":"p1"}'   # LATE_SIDE_OUTPUT

curl -s localhost:8080/api/side-output
```

## 6. 空闲分区与恢复

```bash
curl -s -XPOST localhost:8080/api/partitions/idle -H 'Content-Type: application/json' \
  -d '{"partition":"p1"}'                                      # p1 退出最小值计算
curl -s -XPOST localhost:8080/api/watermark -H 'Content-Type: application/json' \
  -d '{"partition":"p2","watermark":25}'
# 全局水位=25：[10,20) 关闭并直接 purge（25 >= 20+2），count=2

curl -s -XPOST localhost:8080/api/events -H 'Content-Type: application/json' \
  -d '{"id":"e11","key":"a","eventTime":26,"partition":"p1"}' # 事件自动恢复 p1
# p1 旧水位 12 使全局水位仍停在 25；p1=30 后需 p2=30 才推进
```

## 7. 查看状态与重置

```bash
curl -s localhost:8080/api/state      # 全量快照
curl -s localhost:8080/api/results    # 所有窗口发射记录
curl -s -XPOST localhost:8080/api/reset
```

## 错误响应（HTTP 400/404/405）

```bash
curl -i -XPOST localhost:8080/api/watermark -H 'Content-Type: application/json' \
  -d '{"partition":"p1","watermark":5}'      # 400 水位线回退
curl -i -XPOST localhost:8080/api/events -H 'Content-Type: application/json' -d '{bad}'
# 400 JSON 非法或字段缺失
curl -i localhost:8080/api/events            # 405 方法不允许
curl -i localhost:8080/nope                  # 404
```
