"""信封加密服务。

模块划分:
- app.crypto:  信封容器格式、AEAD 加解密、数据密钥包裹/重包裹
- app.store:   主密钥库 (KeyStore) 与对象存储 (ObjectStore, 内存实现)
- app.main:    FastAPI HTTP 接口
"""
