# HTTP 请求样例 (pilab)

启动服务（默认 :8080）：

```bash
go run ./cmd/pilab serve -addr :8080
```

所有样例假设服务运行在 `http://localhost:8080`。

## 1. 健康检查

```bash
curl -s http://localhost:8080/healthz
```

## 2. 列出内置场景

```bash
curl -s http://localhost:8080/api/scenarios
```

## 3. 查看某个场景的配置（GET）与直接运行（POST）

```bash
curl -s http://localhost:8080/api/scenarios/inversion-pip
curl -s -X POST http://localhost:8080/api/scenarios/inversion-pip
```

内置场景 id：

| id                          | 说明                                   |
| --------------------------- | -------------------------------------- |
| `inversion-pip`             | 经典优先级反转，启用优先级继承         |
| `inversion-none`            | 经典优先级反转，无继承（对照组）       |
| `three-level-inheritance`   | 三级嵌套传递继承（9→5→1）              |
| `multi-lock-order`          | 多锁、嵌套获取与释放顺序               |
| `deadlock-abba`             | AB-BA 环形等待死锁反例                 |

## 4. 经典反转有/无 PIP 的量化对比

```bash
curl -s http://localhost:8080/api/compare/inversion
```

## 5. 提交自定义配置运行（返回完整时间线 JSON）

```bash
curl -s -X POST http://localhost:8080/api/simulate \
  -H 'Content-Type: application/json' \
  --data @examples/inversion-pip.json
```

其余配置文件同样可用：

```bash
curl -s -X POST http://localhost:8080/api/simulate \
  -H 'Content-Type: application/json' --data @examples/inversion-none.json
curl -s -X POST http://localhost:8080/api/simulate \
  -H 'Content-Type: application/json' --data @examples/three-level-inheritance.json
curl -s -X POST http://localhost:8080/api/simulate \
  -H 'Content-Type: application/json' --data @examples/multi-lock-order.json
curl -s -X POST http://localhost:8080/api/simulate \
  -H 'Content-Type: application/json' --data @examples/deadlock-abba.json
```

## 6. SSE 流式接收结构化事件

`/api/simulate/stream` 逐条推送 `event: event`（每个状态变更），
最后推送一帧 `event: report`（完整报告）。

```bash
curl -s -N -X POST http://localhost:8080/api/simulate/stream \
  -H 'Content-Type: application/json' \
  --data @examples/three-level-inheritance.json
```

## 7. 错误样例

未知锁引用 → `422 Unprocessable Entity`：

```bash
curl -s -i -X POST http://localhost:8080/api/simulate \
  -H 'Content-Type: application/json' \
  -d '{"locks":[{"id":"A"}],"tasks":[{"id":"t","basePriority":1,"arrival":0,"program":[{"op":"lock","lock":"ZZ"}]}]}'
```

畸形 JSON → `400 Bad Request`：

```bash
curl -s -i -X POST http://localhost:8080/api/simulate \
  -H 'Content-Type: application/json' -d '{nope'
```
