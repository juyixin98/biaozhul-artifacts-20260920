"""撮合引擎异常类型。"""


class TradingError(Exception):
    """交易业务错误基类。"""


class MaintenanceActive(TradingError):
    """维护模式中，停止接单。"""


class IdempotencyConflict(TradingError):
    """同幂等键但参数不同。"""


class OrderNotOpen(TradingError):
    """订单已成交/已撤单，不能再撤或参与撮合。"""


class OrderNotFound(TradingError):
    """订单不存在或不属于当前用户。"""


class InsufficientFunds(TradingError):
    """可用余额不足。"""
