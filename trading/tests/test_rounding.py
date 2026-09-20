"""
舍入规则测试（8 位定点数）：
- 冻结 CEILING；成交金额 FLOOR；手续费 FLOOR；
- 限价买冻结零头在部分成交后退回；
- 极端微小手续费按 0 处理，不会出现负到手数量。
"""
from decimal import Decimal as D

from accounts.models import Account
from accounts.reconciliation import reconcile_snapshot  # noqa: F401
from trading.decimal_utils import ceil8, floor8, quantize
from trading.engine import bootstrap_order_book, engine
from trading.models import Order, Trade

from .base import BaseEngineTestCase


class DecimalRuleTests(BaseEngineTestCase):

    def test_rounding_primitives(self):
        self.assertEqual(ceil8(D("0.000000001")), D("0.00000001"))
        self.assertEqual(ceil8(D("1.234567891")), D("1.23456790"))
        self.assertEqual(floor8(D("0.000000009")), D("0.00000000"))
        self.assertEqual(floor8(D("1.234567899")), D("1.23456789"))
        self.assertEqual(quantize(D("2.5")), D("2.50000000"))

    def test_freeze_ceiling_and_trade_floor_amount(self):
        """价格 10.005 * 数量 0.000001：冻结向上，成交金额向下截断。"""
        # 改 tick 允许三位小数价
        self.pair.tick_size = D("0.001")
        self.pair.save()
        engine.reset_for_tests()
        bootstrap_order_book()

        # bob 卖 0.000001 @ 10.005
        sell, _ = engine.submit_order(
            user=self.bob, pair=self.pair, side="SELL", order_type="LIMIT",
            quantity=D("0.000001"), limit_price=D("10.005"), idem_key="s")
        # alice 买同样数量：10.005*0.000001 = 0.000010005
        buy, _ = engine.submit_order(
            user=self.alice, pair=self.pair, side="BUY", order_type="LIMIT",
            quantity=D("0.000001"), limit_price=D("10.005"), idem_key="b")

        buy.refresh_from_db()
        # 冻结 0.00001001；成交扣 floor(0.000010005)=0.00001000，
        # FILLED 后粉尘 0.00000001 自动退回，订单剩余冻结为 0
        self.assertEqual(buy.remaining_frozen, D("0"))

        trade = Trade.objects.get(taker_order=buy)
        self.assertEqual(trade.quote_amount, D("0.00001000"))  # FLOOR
        # 数量 0.000001，费率 0.001 -> 费用 1e-9 -> FLOOR 到 0
        self.assertEqual(trade.taker_fee, D("0"))

    def test_buyer_never_negative_across_many_small_fills(self):
        """
        跨 20 个不同卖价各成交极小数量，验证 FLOOR 金额累加不会击穿买方冻结，
        且对账始终守恒。
        """
        # 20 个用户各挂 0.000001 BTC，价格 100.00~100.19
        from django.contrib.auth.models import User

        sellers = [self.alice, self.bob, self.carol]
        for i in range(20):
            u = sellers[i % len(sellers)]
            engine.submit_order(
                user=u, pair=self.pair, side="SELL", order_type="LIMIT",
                quantity=D("0.000001"), limit_price=D("100.00") + D(i) * D("0.01"),
                idem_key=f"ask-{i}")

        # bob 用限价 100.20 买 0.000020（CEILING 冻结）
        buy, _ = engine.submit_order(
            user=self.bob, pair=self.pair, side="BUY", order_type="LIMIT",
            quantity=D("0.000020"), limit_price=D("100.20"), idem_key="sweep")
        self.assertEqual(buy.status, Order.Status.FILLED)
        self.assertEqual(buy.filled_qty, D("0.000020"))
        # 买方冻结必须清零（成交金额<=CEILING冻结，dust 退回）
        self.assertEqual(buy.remaining_frozen, D("0"))

        result = reconcile_snapshot()
        self.assertTrue(result["ok"], result["mismatches"])

    def test_fee_charged_on_received_asset(self):
        """买收 base 扣 base 费；卖收 quote 扣 quote 费。"""
        engine.submit_order(
            user=self.alice, pair=self.pair, side="SELL", order_type="LIMIT",
            quantity=D("1"), limit_price=D("50000"), idem_key="ask")
        buy, _ = engine.submit_order(
            user=self.bob, pair=self.pair, side="BUY", order_type="LIMIT",
            quantity=D("1"), limit_price=D("50000"), idem_key="buy")
        trade = Trade.objects.get(taker_order=buy)
        # taker(bob) 买：收 1 BTC，费 0.001 BTC
        self.assertEqual(trade.taker_fee, D("0.001"))
        self.assertEqual(trade.maker_fee, D("50.00000000"))  # 50000*0.001

        fee_btc = Account.objects.get(
            account_type=Account.AccountType.FEE, asset=self.btc)
        fee_usdt = Account.objects.get(
            account_type=Account.AccountType.FEE, asset=self.usdt)
        self.assertEqual(fee_btc.available, D("0.001"))
        self.assertEqual(fee_usdt.available, D("50.00000000"))

    def test_partial_fill_dust_refund(self):
        """限价买单部分成交后重挂：多冻结零头退回，继续保持正确冻结。"""
        # alice 卖 1 @50000
        engine.submit_order(
            user=self.alice, pair=self.pair, side="SELL", order_type="LIMIT",
            quantity=D("1"), limit_price=D("50000"), idem_key="ask")
        # bob 买 3 @50000，冻结 150000，成交 1 后剩余 2 继续挂，冻结 100000
        buy, _ = engine.submit_order(
            user=self.bob, pair=self.pair, side="BUY", order_type="LIMIT",
            quantity=D("3"), limit_price=D("50000"), idem_key="buy")
        buy.refresh_from_db()
        self.assertEqual(buy.status, Order.Status.PARTIALLY_FILLED)
        self.assertEqual(buy.remaining_frozen, D("100000"))
        acc = self.account(self.bob, self.usdt)
        self.assertEqual(acc.frozen, D("100000"))
        self.assertConserved()
