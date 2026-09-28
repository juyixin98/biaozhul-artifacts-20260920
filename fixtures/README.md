# 最小数据夹具（golden byte vectors）

`fixtures/golden/` 存放由**独立于生产解析器**的生成器产生的参考字节：

| 文件 | 内容 |
|------|------|
| `manifest.json` | 每个向量的 boundary、整线 SHA-256、长度、逐部件期望（名称/类型/大小/SHA-256） |
| `v01-field-and-file.bin` | 一个文本字段 + 一个文本文件 |
| `v02-unicode-filename-star.bin` | UTF-8 字段值 + RFC 5987 `filename*`（欧元符号） |
| `v03-near-boundary-in-body.bin` | 文件正文中嵌入“近似边界” `CRLF "--" boundary "X"`，不得误切 |
| `v04-quoted-parameters.bin` | 文件名含引号、分号、空格（quoted-string / quoted-pair） |
| `v05-duplicate-names-and-binary.bin` | 两个同名字段（合法 HTML 形态）+ 99 字节确定性二进制 |

## 参考答案为何可信

- 生成器 `scripts/gen-golden.ts` 用测试专用手写编码器 `test/helpers/encode.ts`
  产出字节，与 `src/` 下的被测实现**零代码共享**；
- 所有期望大小用长度计数、所有 SHA-256 用 Node 内置 `node:crypto` 独立计算；
- 填充字节来自固定种子 LCG（`deterministicBytes(seed, n)`），可逐字节复现。

重新生成（确定性，输出应与已提交文件逐字节一致）：

```bash
npm run golden:generate
```

校验夹具本身（不导入解析器）：`test/golden/golden-vectors.test.ts`
比对磁盘字节与 `wireSha256`/长度，并确认 v03 的近似边界位于真正终止符之前。
