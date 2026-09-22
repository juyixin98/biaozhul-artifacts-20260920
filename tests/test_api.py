"""HTTP/API tests: auth, isolation, admin fee/maintenance endpoints."""
import json
from decimal import Decimal

from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.trading import engine
from apps.trading.book_registry import registry
from apps.trading.models import Order
from tests.base import EngineTestCase
from tests.factories import credit, make_admin, make_market, make_user


class ApiTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.admin = make_admin()
        self.market = make_market()
        credit(self.alice, self.market.base_asset, "10")
        credit(self.bob, self.market.quote_asset, "1000000")
        self.ca = APIClient()
        self.cb = APIClient()
        self.cadmin = APIClient()
        self.ca.force_authenticate(self.alice)
        self.cb.force_authenticate(self.bob)
        self.cadmin.force_authenticate(self.admin)

    def test_register_and_token_login(self):
        anon = APIClient()
        resp = anon.post("/api/auth/register/",
                         {"username": "newbie", "password": "secret123"},
                         format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIn("token", resp.json())

        resp = anon.post(
            "/api/auth/login/",
            HTTP_AUTHORIZATION="Basic " +
            __import__("base64").b64encode(b"newbie:secret123").decode(),
        )
        self.assertEqual(resp.status_code, 200)

    def test_order_lifecycle_via_api(self):
        resp = self.ca.post("/api/orders/", {
            "symbol": "BTC-USDT", "type": "LIMIT", "side": "SELL",
            "price": "50000", "quantity": "1",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        order_id = resp.json()["order"]["id"]

        # Bob matches
        resp = self.cb.post("/api/orders/", {
            "symbol": "BTC-USDT", "type": "LIMIT", "side": "BUY",
            "price": "50000", "quantity": "1",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(len(resp.json()["trades"]), 1)

        # Alice can't cancel a filled order
        resp = self.ca.delete(f"/api/orders/{order_id}/")
        self.assertEqual(resp.status_code, 409)

    def test_user_isolation_orders(self):
        r = engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
        )
        engine.sync_book_after_commit(self.market.id, order=r.order)

        # Bob can't see Alice's order in list
        resp = self.cb.get("/api/orders/")
        self.assertEqual(resp.json()["count"], 0)

        # Bob can't fetch it by id
        resp = self.cb.get(f"/api/orders/{r.order.id}/")
        self.assertEqual(resp.status_code, 404)

        # Bob can't cancel it
        resp = self.cb.delete(f"/api/orders/{r.order.id}/")
        self.assertEqual(resp.status_code, 404)

        # Alice sees it
        resp = self.ca.get("/api/orders/")
        self.assertEqual(resp.json()["count"], 1)

    def test_balances_only_own(self):
        resp = self.ca.get("/api/balances/")
        codes = {row["asset"] for row in resp.json()["results"]}
        self.assertIn("BTC", codes)
        resp = self.cb.get("/api/balances/")
        codes = {row["asset"] for row in resp.json()["results"]}
        self.assertNotIn("BTC", codes)
        self.assertIn("USDT", codes)

    def test_idempotent_api_calls(self):
        body = {
            "symbol": "BTC-USDT", "type": "LIMIT", "side": "SELL",
            "price": "50000", "quantity": "1", "idempotency_key": "abc-1",
        }
        r1 = self.ca.post("/api/orders/", body, format="json")
        r2 = self.ca.post("/api/orders/", body, format="json")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json()["replayed"])
        self.assertEqual(
            r1.json()["order"]["id"], r2.json()["order"]["id"]
        )

        # Same key, different quantity -> 409
        body["quantity"] = "2"
        r3 = self.ca.post("/api/orders/", body, format="json")
        self.assertEqual(r3.status_code, 409)

    def test_admin_fee_update_and_audit_log(self):
        resp = self.cadmin.patch(
            "/api/admin/markets/BTC-USDT/fees/",
            {"maker_fee_bps": "5", "taker_fee_bps": "15"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.market.refresh_from_db()
        self.assertEqual(self.market.maker_fee_bps, Decimal("5"))
        self.assertEqual(self.market.taker_fee_bps, Decimal("15"))

        # Non-admin forbidden
        resp = self.cb.patch(
            "/api/admin/markets/BTC-USDT/fees/",
            {"maker_fee_bps": "1"}, format="json",
        )
        self.assertEqual(resp.status_code, 403)

        # Audit log visible to admin
        resp = self.cadmin.get("/api/admin/audit-logs/?action=FEE_UPDATE")
        self.assertEqual(resp.json()["count"], 1)
        # ...and hidden from normal users
        resp = self.cb.get("/api/admin/audit-logs/")
        self.assertEqual(resp.status_code, 403)

    def test_admin_maintenance_endpoint(self):
        r = engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
        )
        engine.sync_book_after_commit(self.market.id, order=r.order)

        resp = self.cadmin.post(
            "/api/admin/markets/BTC-USDT/maintenance/",
            {"enabled": True}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["in_maintenance"])
        self.assertEqual(resp.json()["canceled_order_count"], 1)

        # New order rejected via API
        resp = self.cb.post("/api/orders/", {
            "symbol": "BTC-USDT", "type": "LIMIT", "side": "BUY",
            "price": "50000", "quantity": "1",
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("maintenance", resp.json()["detail"])

    def test_orderbook_endpoint(self):
        r = engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
        )
        engine.sync_book_after_commit(self.market.id, order=r.order)
        anon = APIClient()
        resp = anon.get("/api/markets/BTC-USDT/orderbook/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["asks"], [["50000.00000000", "1.00000000"]])
        self.assertEqual(data["bids"], [])

    def test_unknown_symbol_404(self):
        resp = self.ca.post("/api/orders/", {
            "symbol": "NOPE/USDT", "type": "LIMIT", "side": "SELL",
            "price": "1", "quantity": "1",
        }, format="json")
        self.assertEqual(resp.status_code, 404)
