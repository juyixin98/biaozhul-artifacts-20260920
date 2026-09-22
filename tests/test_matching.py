"""Core matching: price-time priority, partial fills, market orders."""
from decimal import Decimal

from apps.accounts.models import Balance
from apps.trading import engine
from apps.trading.book_registry import registry
from apps.trading.models import Order, Trade
from tests.base import EngineTestCase
from tests.factories import balances, credit, make_market, make_user


class MatchingTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.carol = make_user("carol")
        self.market = make_market()
        self.btc = self.market.base_asset
        self.usdt = self.market.quote_asset
        credit(self.alice, self.btc, "100")
        credit(self.bob, self.usdt, "10000000")
        credit(self.carol, self.usdt, "10000000")
        credit(self.carol, self.btc, "100")

    def _book(self):
        return registry.get(self.market.id)

    def test_limit_buy_matches_limit_sell_full(self):
        # Alice sells 1 BTC at 50000
        sell = engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
        ).order
        engine.sync_book_after_commit(self.market.id, order=sell)
        self.assertEqual(self._book().asks.depth(),
                         [(Decimal("50000.00000000"), Decimal("1.00000000"))])

        # Bob buys 1 at 50000 -> full match
        report = engine.submit_order(
            user=self.bob, market=self.market, type="LIMIT",
            side="BUY", price=Decimal("50000"), quantity=Decimal("1"),
        )
        engine.apply_book_changes(self.market.id, report)

        sell.refresh_from_db()
        buy = report.order
        self.assertEqual(sell.status, Order.Status.FILLED)
        self.assertEqual(buy.status, Order.Status.FILLED)
        self.assertEqual(Trade.objects.count(), 1)
        trade = report.trades[0]
        self.assertEqual(trade.price, Decimal("50000.00000000"))
        self.assertEqual(trade.quantity, Decimal("1.00000000"))

        # maker fee 10bps on 50000 quote = 50 quote; taker fee 20bps on
        # 1 base = 0.002 base
        self.assertEqual(trade.maker_fee, Decimal("50.00000000"))
        self.assertEqual(trade.taker_fee, Decimal("0.00200000"))

        # Alice: spent 1 frozen BTC, received 50000-50 quote
        a = balances(self.alice)
        self.assertEqual(a["BTC"], (Decimal("99.00000000"), Decimal("0")))
        self.assertEqual(a["USDT"], (Decimal("49950.00000000"), Decimal("0")))
        # Bob: spent 50000 frozen quote, received 1-0.002 BTC
        b = balances(self.bob)
        self.assertEqual(b["USDT"], (Decimal("9950000.00000000"), Decimal("0")))
        self.assertEqual(b["BTC"], (Decimal("0.99800000"), Decimal("0")))

    def test_price_priority_best_ask_first(self):
        # Two asks: alice 50100, carol 49900; carol must fill first.
        for user, price in ((self.alice, "50100"), (self.carol, "49900")):
            r = engine.submit_order(
                user=user, market=self.market, type="LIMIT",
                side="SELL", price=Decimal(price), quantity=Decimal("1"),
            )
            engine.sync_book_after_commit(self.market.id, order=r.order)

        report = engine.submit_order(
            user=self.bob, market=self.market, type="LIMIT",
            side="BUY", price=Decimal("50200"), quantity=Decimal("1"),
        )
        engine.apply_book_changes(self.market.id, report)
        self.assertEqual(report.trades[0].maker_order.user, self.carol)
        # Bob's buy rests with 0? it was full size 1 -> filled
        self.assertEqual(report.order.status, Order.Status.FILLED)
        # Alice's ask is still resting untouched
        alice_order = Order.objects.get(user=self.alice)
        self.assertEqual(alice_order.status, Order.Status.NEW)
        self.assertEqual(alice_order.filled_quantity, Decimal("0"))

    def test_time_priority_same_price_fifo(self):
        # Three same-price 1-BTC sells, submitted in order; a 2.5 BTC buy
        # hits first and second fully, third for 0.5.
        sellers = []
        for i, user in enumerate((self.alice, self.carol, make_user("dave"))):
            if user.username == "dave":
                credit(user, self.btc, "10")
            r = engine.submit_order(
                user=user, market=self.market, type="LIMIT",
                side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
            )
            engine.sync_book_after_commit(self.market.id, order=r.order)
            sellers.append(r.order)

        report = engine.submit_order(
            user=self.bob, market=self.market, type="LIMIT",
            side="BUY", price=Decimal("50000"), quantity=Decimal("2.5"),
        )
        engine.apply_book_changes(self.market.id, report)

        sellers[0].refresh_from_db()
        sellers[1].refresh_from_db()
        sellers[2].refresh_from_db()
        self.assertEqual(sellers[0].status, Order.Status.FILLED)
        self.assertEqual(sellers[1].status, Order.Status.FILLED)
        self.assertEqual(sellers[2].status, Order.Status.PARTIALLY_FILLED)
        self.assertEqual(sellers[2].filled_quantity, Decimal("0.50000000"))
        # taker rests for 0? 2.5 fully matched -> filled
        self.assertEqual(report.order.status, Order.Status.FILLED)
        # book still contains dave's 0.5
        self.assertEqual(self._book().asks.depth(),
                         [(Decimal("50000.00000000"), Decimal("0.50000000"))])

    def test_partial_fill_taker_rests_and_releases_extra_freeze(self):
        # Only 0.4 BTC on offer; Bob's 1 BTC buy fills 0.4 and rests 0.6.
        r = engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("0.4"),
        )
        engine.sync_book_after_commit(self.market.id, order=r.order)

        report = engine.submit_order(
            user=self.bob, market=self.market, type="LIMIT",
            side="BUY", price=Decimal("50000"), quantity=Decimal("1"),
        )
        engine.apply_book_changes(self.market.id, report)
        buy = report.order
        buy.refresh_from_db()
        self.assertEqual(buy.status, Order.Status.PARTIALLY_FILLED)
        self.assertEqual(buy.filled_quantity, Decimal("0.40000000"))
        # Remaining freeze must equal exactly 50000 * 0.6 = 30000
        self.assertEqual(buy.frozen_remaining, Decimal("30000.00000000"))
        # Book has the resting buy on the bid side.
        self.assertEqual(self._book().bids.depth(),
                         [(Decimal("50000.00000000"), Decimal("0.60000000"))])

        # Bob's available USDT = 10m - freeze 50000 + released nothing else
        b = balances(self.bob)
        # 20000 was released back from the 0.4 fill path? Freeze was 50000;
        # fill consumed floor(50000*0.4)=20000; rest reconciliation moves
        # (50000-20000)-30000 = 0.
        self.assertEqual(b["USDT"], (Decimal("9950000.00000000"), Decimal("30000.00000000")))

    def test_market_sell_remainer_canceled_and_released(self):
        # Only 0.3 BTC bid; alice (needs BTC) ... here carol bids 0.3.
        r = engine.submit_order(
            user=self.carol, market=self.market, type="LIMIT",
            side="BUY", price=Decimal("49000"), quantity=Decimal("0.3"),
        )
        engine.sync_book_after_commit(self.market.id, order=r.order)

        report = engine.submit_order(
            user=self.alice, market=self.market, type="MARKET",
            side="SELL", quantity=Decimal("1"),
        )
        engine.apply_book_changes(self.market.id, report)
        sell = report.order
        sell.refresh_from_db()
        self.assertEqual(sell.status, Order.Status.CANCELED)
        self.assertEqual(sell.filled_quantity, Decimal("0.30000000"))
        self.assertEqual(sell.frozen_remaining, Decimal("0"))
        a = balances(self.alice)
        # 100 BTC: 0.3 sold, 0.7 released back
        self.assertEqual(a["BTC"], (Decimal("99.70000000"), Decimal("0")))

    def test_market_buy_consumes_budget(self):
        # Alice sells 2 BTC at 50000.
        r = engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("2"),
        )
        engine.sync_book_after_commit(self.market.id, order=r.order)

        # Bob market-buys with 75000 USDT budget -> 1.5 BTC exactly.
        report = engine.submit_order(
            user=self.bob, market=self.market, type="MARKET",
            side="BUY", quote_amount=Decimal("75000"),
        )
        engine.apply_book_changes(self.market.id, report)
        buy = report.order
        buy.refresh_from_db()
        self.assertEqual(buy.status, Order.Status.FILLED)
        self.assertEqual(buy.filled_quantity, Decimal("1.50000000"))
        self.assertEqual(buy.frozen_remaining, Decimal("0"))
        b = balances(self.bob)
        # taker fee in base: 20bps * 1.5 = 0.003
        self.assertEqual(b["BTC"], (Decimal("1.49700000"), Decimal("0")))
        self.assertEqual(b["USDT"], (Decimal("9925000.00000000"), Decimal("0")))

    def test_market_buy_no_liquidity_is_canceled_and_refunded(self):
        report = engine.submit_order(
            user=self.bob, market=self.market, type="MARKET",
            side="BUY", quote_amount=Decimal("100"),
        )
        engine.apply_book_changes(self.market.id, report)
        order = report.order
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELED)
        self.assertEqual(order.filled_quantity, Decimal("0"))
        b = balances(self.bob)
        self.assertEqual(b["USDT"], (Decimal("10000000.00000000"), Decimal("0")))

    def test_insufficient_funds_rejected(self):
        dave = make_user("dave")
        credit(dave, self.usdt, "10")
        with self.assertRaises(engine.OrderRejected):
            engine.submit_order(
                user=dave, market=self.market, type="LIMIT",
                side="BUY", price=Decimal("50000"), quantity=Decimal("1"),
            )
        # No order, no freeze left behind
        self.assertEqual(Order.objects.filter(user=dave).count(), 0)
        d = balances(dave)
        self.assertEqual(d["USDT"], (Decimal("10.00000000"), Decimal("0")))

    def test_rejection_in_maintenance(self):
        engine.set_maintenance(
            admin_user=self.bob, market_id=self.market.id, enabled=True
        )
        with self.assertRaises(engine.OrderRejected):
            engine.submit_order(
                user=self.alice, market=self.market, type="LIMIT",
                side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
            )
