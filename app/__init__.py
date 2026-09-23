# -*- coding-utf-8 -*-
"""IBC 包超时状态模型（教学用简化实现）应用包。

模块概览：
- encoding: 十六进制/base64 编码、整数与规范序列化
- crypto:   SHA-256 哈希、Ed25519 真实签名/验签
- smt:      256 层稀疏 Merkle 状态树（IBC 状态存在性/不存在性证明）
- storage:  SQLite 表结构与连接
- state:    链/客户端/通道/数据包的状态机
- engine:   状态机对外的事务操作
- api:      FastAPI HTTP 接口
"""
