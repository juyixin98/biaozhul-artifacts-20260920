# 请求样例与真实响应

以下响应均为 2026-09-24 在本机（OpenJDK 21.0.12）对运行中的服务实际执行
curl 后保存的原始输出（经 `python3 -m json.tool` 美化，内容未改动）。
服务当时监听临时端口 41637（8080/18080 被本机其他进程占用）。

## 1. 健康检查

```bash
curl -s http://127.0.0.1:41637/health
```

→ [health.json](health.json)

## 2. ß 大小写展开：查询 "strasse"

```bash
curl -s -X POST http://127.0.0.1:41637/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"strasse"}'
```

→ [search-strasse.json](search-strasse.json)

4 个命中：`Straße`（两次）、`STRASSE`、`strasse`。注意首个命中
`startUtf16:0, endUtf16:6` —— 规范化键上 7 个字符的 `strasse` 映射回原文
6 个 UTF-16 单元；`endUtf8:7` 因为 `ß` 是 2 字节。

## 3. 组合字符：NFD 查询 "Café"（e + U+0301）

```bash
curl -s -X POST http://127.0.0.1:41637/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"Café"}'
```

→ [search-cafe-nfd.json](search-cafe-nfd.json)

5 个命中，涵盖 NFC 的 `Café`/`café`/`CAFÉ` 与语料中 NFD 形式的 `café`
（最后一个命中 `endUtf16:58 - startUtf16:53 = 5`，完整覆盖 `e`+组合符两个
码点，未切断组合序列）。

## 4. 连字展开：查询 "ﬁle"（U+FB01 + le）

```bash
curl -s 'http://127.0.0.1:41637/search?q=%EF%AC%81le'
```

→ [search-ligature-file.json](search-ligature-file.json)

`normalizedQuery` 为 `file`；命中映射回原文 3 个 UTF-16 单元的 `ﬁle`
（`startUtf8:4, endUtf8:9`：连字 ﬁ 是 3 字节）。

## 5. 多字节/代理对：查询 "abc"（带 limit）

```bash
curl -s -X POST http://127.0.0.1:41637/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"abc","limit":10}'
```

→ [search-abc.json](search-abc.json)

两个命中：全角 `ＡＢＣ`（3 码点 / 9 UTF-8 字节）与数学粗体 `𝐀𝐁𝐂`
（3 码点 = 6 UTF-16 单元 = 12 UTF-8 字节，区间完整覆盖三个代理对）。

## 6. 规范化查看

```bash
curl -s 'http://127.0.0.1:41637/normalize?text=Stra%C3%9Fe'
```

→ [normalize-strasse.json](normalize-strasse.json)：`Straße` → `strasse`
（原文 6 码点 → 规范化 7 字符）。
