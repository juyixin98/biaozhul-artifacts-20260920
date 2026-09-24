"""SBOM 风险匹配服务（纯后端）。

模块划分：
- semver:   SemVer 2.0.0 解析与优先级比较
- ranges:   自定义 SemVer 区间语法（AND 组合、^/~、比较符、预发布策略）
- models:   自定义 SBOM / 漏洞馈送 JSON 的 pydantic 模型
- graph:    包依赖图、环检测、路径枚举
- feed:     漏洞馈送加载与 Ed25519 分离签名校验（cryptography）
- matching: 风险匹配主逻辑
- app:      FastAPI HTTP 接口
"""

__version__ = "1.0.0"
