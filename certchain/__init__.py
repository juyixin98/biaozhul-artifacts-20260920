"""离线证书链验证服务。

纯后端、纯本地：基于 cryptography 库的 x509.verification 模块做
RFC 5280 风格的路径构建与验证。不联网补证书、不查询吊销。
"""

__version__ = "0.1.0"
