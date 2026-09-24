# 示例输入

这些文件由 `scripts/acceptance.sh` 在运行时用**真实数据 + 真实 SHA-256** 生成，
因此仓库不预置会过期的摘要。生成后：

- `examples/generated/cfgA.json`、`cfgB.json` —— 两个镜像的 config blob
- `examples/generated/layer-shared.bin` —— 两个镜像共享的层
- `examples/generated/layer-a.bin`、`layer-b.bin` —— 各自私有层
- `examples/generated/manifest-alpha.json`、`manifest-beta.json` —— OCI image manifest

`examples/manifest.template.json` 是清单结构模板（字段含义见下）。
