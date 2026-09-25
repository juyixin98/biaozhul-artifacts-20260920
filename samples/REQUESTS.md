# HTTP 请求样例

所有接口仅监听本机 `127.0.0.1`，不调用任何外部服务。默认端口 `8080`，
可用 `scripts/serve.sh 0` 让系统分配空闲端口（端口打印在 stderr 日志中）。

## 1. 健康检查 / 算法参数

```bash
curl -s http://127.0.0.1:8080/health
```

返回：

```json
{"lshBands":60,"minhashNumHashes":120,"status":"ok","minhashSeed":20260924,
 "service":"near-duplicate-clustering","shingleK":3,"lshRows":2}
```

## 2. 查看内置合成语料（21 篇）

```bash
curl -s http://127.0.0.1:8080/corpus | python3 -m json.tool --no-ensure-ascii
```

## 3. 对内置合成语料聚类（默认阈值 0.6）

请求体见 [`cluster-default.json`](cluster-default.json)：

```bash
curl -s -X POST http://127.0.0.1:8080/cluster \
  -H 'Content-Type: application/json' \
  -d @samples/cluster-default.json | python3 -m json.tool --no-ensure-ascii
```

`POST /cluster` 带空体 `{}` 等价于对内置语料用默认阈值聚类。

## 4. 提交自定义文本

请求体见 [`cluster-custom-texts.json`](cluster-custom-texts.json)：

```bash
curl -s -X POST http://127.0.0.1:8080/cluster \
  -H 'Content-Type: application/json' \
  -d @samples/cluster-custom-texts.json
```

## 5. 不开服务，直接命令行跑一遍

```bash
scripts/run.sh 0.6           # JSON 报告打到 stdout，也保存在 results/ 下
```

## 返回结构说明

- `stats`：候选召回与复核统计（候选数、通过数、误报数、对暴力精确真相的召回率/精度）。
- `clusters[]`：多文档连通分量（聚类）。
  - `edges[]`：该簇内精确 Jaccard ≥ 阈值的直接边。
  - `belowThresholdPairs[]`：**同簇但这一对本身低于阈值**的点对
    （传递链证据，例如 `c1`/`c3`），用以明确“同簇 ≠ 每对都超阈值”。
- `singletons`：未与任何文档合并的文档（含空 shingle 的短文档）。
- `candidates[]`：LSH 召回的每一对，带 `exactJaccard`、`minhashEstimate`、
  `bandHits` 与 `accepted`；`accepted=false` 即被精确复核拦下的误报候选。
- `missedTrueEdges`：候选阶段漏掉的真边（本语料为 0）。
