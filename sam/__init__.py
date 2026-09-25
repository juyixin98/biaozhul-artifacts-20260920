"""SAM — Signed Artifact Manifest (签名制品清单) 本地安全数据处理服务.

纯后端包, 不依赖任何网络账户:
- 规范化 JSON:       sam.canonical
- 路径安全:          sam.safepaths
- 密钥与信任库:      sam.keys
- 清单构建/封套:     sam.manifest
- 签名 / 验证:       sam.sign / sam.verify
- 执行:              sam.runner
- 命令行:            sam.cli
- 本地 HTTP 服务:    sam.service
"""

__version__ = "1.0.0"
