# HTTP 请求样例

以下样例假设服务以 `./run.sh 8080 data/sample.csv` 启动并自动加载了样例数据集
（20 行，行 ID 0..19，对应 CSV 中数据行的顺序，不含表头）。

行 ID 语义：**行 ID = 加载顺序的 0 基下标**。删除是软删除（只清存活掩码位），
行 ID 不重排、不移位；`NOT x` 恒等于 `存活行 AND (NOT x)`。

---

## 0. 接口说明 / 健康检查

```bash
curl -s http://localhost:8080/ | json_pp
curl -s http://localhost:8080/health
```

## 1. 加载数据集（整体替换；会按新数据重新从行 0 编号）

### 1a. 内嵌 CSV

```bash
curl -s -X POST http://localhost:8080/load \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary '{"csv":"city,grade\nBJ,A\nSH,B\n"}'
```

### 1b. 服务器本地文件路径

```bash
curl -s -X POST http://localhost:8080/load \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary '{"csvPath":"/absolute/path/to/data/sample.csv"}'
```

### 1c. 结构化 JSON（columns + rows）

```bash
curl -s -X POST http://localhost:8080/load \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary '{
    "columns": ["city", "grade"],
    "rows": [["BJ","A"], ["BJ","B"], ["SH","A"]]
  }'
```

## 2. 查询（AND / OR / NOT / IN / 存活全集）

### 2a. 等值：city = BJ

```bash
curl -s -X POST http://localhost:8080/query \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary '{"where":{"op":"eq","col":"city","value":"BJ"}}'
```

### 2b. IN：city ∈ {BJ, SZ}

```bash
curl -s -X POST http://localhost:8080/query \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary '{"where":{"op":"in","col":"city","values":["BJ","SZ"]}}'
```

### 2c. AND：(city = BJ AND active = true) OR grade = A

```bash
curl -s -X POST http://localhost:8080/query \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary '{
    "where": {
      "op": "or",
      "args": [
        {"op":"and","args":[
          {"op":"eq","col":"city","value":"BJ"},
          {"op":"eq","col":"active","value":"true"}
        ]},
        {"op":"eq","col":"grade","value":"A"}
      ]
    },
    "limit": 100
  }'
```

### 2d. NOT（仅在存活行全集内取补）：NOT(city = BJ)

```bash
curl -s -X POST http://localhost:8080/query \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary '{"where":{"op":"not","arg":{"op":"eq","col":"city","value":"BJ"}}}'
```

### 2e. 当前存活全集

```bash
curl -s -X POST http://localhost:8080/query \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary '{"where":{"op":"alive"}}'
```

### 2f. 不存在的值（正常返回空，不是错误）

```bash
curl -s -X POST http://localhost:8080/query \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary '{"where":{"op":"eq","col":"city","value":"NYC"}}'
```

## 3. 删除（软删除，幂等）

### 3a. 按行 ID 删除

```bash
curl -s -X POST http://localhost:8080/delete \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary '{"ids":[0,2,4]}'
```

### 3b. 按表达式删除（grade = C）

```bash
curl -s -X POST http://localhost:8080/delete \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary '{"where":{"op":"eq","col":"grade","value":"C"}}'
```

删除后再查 2d（NOT city=BJ）：已删除行不会被 NOT 取补“捞回来”，
返回的行 ID 仍是它们在数据集中的原始编号。

## 4. 恢复（取消删除，幂等）

```bash
curl -s -X POST http://localhost:8080/restore \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary '{"ids":[0,2]}'

curl -s -X POST http://localhost:8080/restore \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary '{"where":{"op":"eq","col":"grade","value":"C"}}'
```

## 5. 索引空间统计（未压缩 long[] / RLE 压缩 / 字典 / 掩码）

```bash
curl -s http://localhost:8080/stats | json_pp
```

## 6. 错误处理样例

```bash
# 未加载数据集 -> 409
curl -s -i -X POST http://localhost:8080/query \
  --data-binary '{"where":{"op":"alive"}}'
# 坏 JSON -> 400
curl -s -i -X POST http://localhost:8080/query --data-binary '{bad'
# 未知列 -> 400
curl -s -i -X POST http://localhost:8080/query \
  --data-binary '{"where":{"op":"eq","col":"nope","value":"x"}}'
# ids 与 where 同时出现 -> 400
curl -s -i -X POST http://localhost:8080/delete \
  --data-binary '{"ids":[0],"where":{"op":"alive"}}'
# 未知路径 -> 404；错误方法 -> 405
curl -s -i http://localhost:8080/nope
```
