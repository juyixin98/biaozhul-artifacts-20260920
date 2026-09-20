"""HTTP API 测试：注册/登录、下单、用户隔离、管理员权限、状态码语义。"""
from decimal import Decimal as D

from rest_framework.test import APITestCase

from accounts.services import issue_simulated_asset
from trading.engine import bootstrap_order_book, engine
from trading.models import Asset, SystemConfig, TradingPair


class ApiTests(APITestCase):

    def setUp(self):
        engine.reset_for_tests()
        self.usdt, _ = Asset.objects.get_or_create(symbol="USDT")
        self.btc, _ = Asset.objects.get_or_create(symbol="BTC")
        self.pair, _ = TradingPair.objects.get_or_create(
            symbol="BTCUSDT",
            defaults={
                "base": self.btc, "quote": self.usdt,
                "tick_size": D("0.01"), "lot_size": D("0.000001"),
                "min_notional": D("0"),
            },
        )
        bootstrap_order_book()
        self.alice = self._register("alice", "pw-strong")
        self.bob = self._register("bob", "pw-strong")
        for u in (self.alice, self.bob):
            issue_simulated_asset(u, self.usdt, D("1000000"))
            issue_simulated_asset(u, self.btc, D("10"))

    def _register(self, username, password):
        from django.contrib.auth.models import User

        return User.objects.create_user(username, password=password)

    def auth(self, user):
        from rest_framework.authtoken.models import Token

        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    def test_register_and_login_flow(self):
        resp = self.client.post("/api/auth/register",
                                {"username": "carol", "password": "secret123"},
                                format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertIn("token", resp.json())

        self.client.credentials()
        resp = self.client.post("/api/auth/login",
                                {"username": "carol", "password": "secret123"},
                                format="json")
        self.assertEqual(resp.status_code, 200)

    def test_unauthenticated_rejected(self):
        self.client.credentials()
        resp = self.client.get("/api/accounts/me")
        self.assertEqual(resp.status_code, 401)

    def test_user_only_sees_own_assets_and_orders(self):
        self.auth(self.alice)
        engine.submit_order(
            user=self.alice, pair=self.pair, side="BUY", order_type="LIMIT",
            quantity=D("1"), limit_price=D("40000"), idem_key="a1")
        engine.submit_order(
            user=self.bob, pair=self.pair, side="BUY", order_type="LIMIT",
            quantity=D("1"), limit_price=D("40000"), idem_key="b1")

        resp = self.client.get("/api/accounts/me")
        self.assertEqual(resp.status_code, 200)
        symbols = {row["asset"] for row in resp.json()}
        self.assertEqual(symbols, {"USDT", "BTC"})

        resp = self.client.get("/api/orders/mine")
        orders = resp.json()
        self.assertEqual(len(orders), 1)

        # alice 直接访问 bob 的订单 -> 404
        from trading.models import Order

        bob_order = Order.objects.get(user=self.bob)
        resp = self.client.get(f"/api/orders/{bob_order.pk}")
        self.assertEqual(resp.status_code, 404)

    def test_place_limit_order_via_api(self):
        self.auth(self.bob)
        resp = self.client.post("/api/orders", {
            "pair": "BTCUSDT", "side": "BUY", "type": "LIMIT",
            "quantity": "1", "limit_price": "40000.00",
            "idempotency_key": "http-1",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["status"], "NEW")

    def test_idempotent_post_returns_200_same_order(self):
        self.auth(self.bob)
        payload = {"pair": "BTCUSDT", "side": "BUY", "type": "LIMIT",
                   "quantity": "1", "limit_price": "40000.00",
                   "idempotency_key": "http-2"}
        r1 = self.client.post("/api/orders", payload, format="json")
        r2 = self.client.post("/api/orders", payload, format="json")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.json()["id"], r2.json()["id"])

    def test_same_key_different_params_409(self):
        self.auth(self.bob)
        base = {"pair": "BTCUSDT", "side": "BUY", "type": "LIMIT",
                "idempotency_key": "http-3"}
        self.client.post("/api/orders",
                         {**base, "quantity": "1", "limit_price": "40000.00"},
                         format="json")
        resp = self.client.post("/api/orders",
                                {**base, "quantity": "2", "limit_price": "40000.00"},
                                format="json")
        self.assertEqual(resp.status_code, 409)

    def test_bad_tick_400(self):
        self.auth(self.bob)
        resp = self.client.post("/api/orders", {
            "pair": "BTCUSDT", "side": "BUY", "type": "LIMIT",
            "quantity": "1", "limit_price": "40000.001",
        }, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_cancel_via_api(self):
        self.auth(self.alice)
        o, _ = engine.submit_order(
            user=self.alice, pair=self.pair, side="BUY", order_type="LIMIT",
            quantity=D("1"), limit_price=D("40000"), idem_key="c")
        resp = self.client.post(f"/api/orders/{o.pk}/cancel")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "CANCELED")

    def test_pairs_listing(self):
        resp = self.client.get("/api/pairs")
        self.assertEqual(resp.status_code, 401)
        self.auth(self.alice)
        resp = self.client.get("/api/pairs")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["symbol"], "BTCUSDT")

    def test_admin_fee_config_requires_admin(self):
        self.auth(self.bob)
        resp = self.client.get("/api/admin/config")
        self.assertEqual(resp.status_code, 403)

    def test_admin_updates_fee_and_audits(self):
        admin = self._register("root", "pw-strong")
        admin.is_staff = True
        admin.is_superuser = True
        admin.save()
        self.auth(admin)

        resp = self.client.patch("/api/admin/config",
                                 {"taker_fee_rate": "0.00050000"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["taker_fee_rate"], "0.00050000")

        resp = self.client.get("/api/admin/audit")
        self.assertEqual(resp.status_code, 200)
        actions = {row["action"] for row in resp.json()}
        self.assertIn("FEE_CONFIG_UPDATE", actions)

    def test_maintenance_blocks_new_orders_503(self):
        admin = self._register("root2", "pw-strong")
        admin.is_staff = True
        admin.is_superuser = True
        admin.save()
        self.auth(admin)
        resp = self.client.post("/api/admin/maintenance/on")
        self.assertEqual(resp.status_code, 200)

        self.auth(self.bob)
        resp = self.client.post("/api/orders", {
            "pair": "BTCUSDT", "side": "BUY", "type": "LIMIT",
            "quantity": "1", "limit_price": "40000.00",
        }, format="json")
        self.assertEqual(resp.status_code, 503)

        self.auth(admin)
        resp = self.client.post("/api/admin/maintenance/off")
        self.assertEqual(resp.status_code, 200)

    def test_reconcile_admin_only(self):
        admin = self._register("root3", "pw-strong")
        admin.is_staff = True
        admin.is_superuser = True
        admin.save()
        self.auth(admin)
        resp = self.client.get("/api/admin/reconcile")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
