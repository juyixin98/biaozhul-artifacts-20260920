# 请求样例

每个文件都是一个可直接提交的 JSON 请求（目录里的引擎是单进程内存态，
因此需要多步的例子用 `"op":"batch"` 打包）。

```bash
java -jar ../hllengine.jar --file 01-sketch-quickstart.json --pretty
```

| 文件 | 演示内容 |
|------|----------|
| `01-sketch-quickstart.json` | 建草图 → 重复插入 → 估计 → 导出 → 列出（看到估计值与误差列） |
| `02-shard-merge.json` | 三个有重叠键的分片合并成全局 UV，并导出合并结果 |
| `03-dataset-query.json` | 建内存表、explain、分组聚合（精确 UV vs 近似 UV）、投影+过滤+limit |
| `05-incompatible-merge.json` | 精度/种子不同的合并被拒（batch 内对应 item 返回 INCOMPATIBLE_CONFIG） |
| `bad/` | 7 个坏输入样例，每个都会返回 `{ok:false}` 且退出码为 2 |

`bad/` 期望的错误码：

```
01-precision-out-of-range.json -> BAD_FORMAT / 精度越界
02-unsupported-hash.json       -> UNSUPPORTED（只接受 MURMUR3_X64_128）
03-sketch-not-found.json       -> NOT_FOUND
04-unknown-op.json             -> BAD_REQUEST
05-bad-base64.json             -> BAD_FORMAT
06-filter-missing-value.json   -> BAD_FORMAT
07-malformed-json.txt          -> BAD_FORMAT（JSON 文本本身非法）
```

单独运行某个坏样例：

```bash
java -jar ../hllengine.jar --file bad/02-unsupported-hash.json
# {"ok":false,"error":{"code":"UNSUPPORTED","message":"unsupported hashId ..."}}
```
