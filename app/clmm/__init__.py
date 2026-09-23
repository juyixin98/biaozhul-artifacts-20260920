"""集中流动性报价引擎（纯后端）。

模块：
- constants: 项目自定义定点常量
- pricing:   tick <-> sqrtPriceX96 价格映射（整数 tick 网格）
- engine:    纯函数报价引擎，跨多个流动性区间逐段扣费
- decimalref: 用 Decimal 的慢速独立参考实现，用于测试交叉验证
- crypto:    快照哈希与报价 HMAC 签名
"""

__all__ = ["constants", "pricing", "engine", "decimalref", "crypto"]
