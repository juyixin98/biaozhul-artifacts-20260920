"""签名制品清单（Signed Artifact Manifest）纯后端服务。

只依赖 Python 标准库与 cryptography；不包含任何前端组件。
"""

__version__ = "1.0.0"

MANIFEST_TYPE = "artifact-manifest/v1"
MANIFEST_VERSION = 1
SIGNATURE_ALGORITHM = "ed25519"
DIGEST_ALGORITHM = "sha256"
