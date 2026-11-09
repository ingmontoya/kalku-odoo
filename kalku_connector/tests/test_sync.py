import json
from datetime import timedelta
from unittest.mock import patch

import requests
from requests.structures import CaseInsensitiveDict

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.kalku_connector.models import kalku_sync
from odoo.addons.kalku_connector.models.kalku_client import (
    KalkuAuthError,
    KalkuClient,
    KalkuError,
)

from .common import KalkuFixtureCase, requires_sale_stock

BASE = "https://app.kalku.co"
PING = "/api/connector/ping"
SALES = "/api/connector/sales"
INVENTORY = "/api/connector/inventory"
SUPPLIER_TERMS = "/api/connector/supplier-terms"
COMPLETE = "/api/connector/sync/complete"
WATERMARK = "kalku.last_sync_at.sales"
WATERMARK_SOURCE = "kalku.last_sync_source.sales"
INACTIVE_OWNER = (
    "El usuario dueño de este token está inactivo. "
    "Genera un token nuevo en Kalku > Integraciones."
)


def response(status, body=None, headers=None):
    answer = requests.Response()
    answer.status_code = status
    answer._content = b"" if body is None else json.dumps(body).encode()
    answer.headers = CaseInsensitiveDict(headers or {})
    answer.encoding = "utf-8"
    return answer


class FakeKalku:
    """Answers like Kalku's connector API and records every call.

    ``answers`` maps ``(path, n)`` to the response of the n-th call (1-based)
    to that path; every other call gets Kalku's normal answer.
    """

    def __init__(self, sales_source_loaded=None, answers=None):
        self.sales_source_loaded = sales_source_loaded
        self.answers = answers or {}
        self.calls = []

    def __call__(self, method, url, **kwargs):
        path = url[len(BASE):]
        payload = kwargs.get("json")
        self.calls.append({"method": method, "path": path, "json": payload, "kwargs": kwargs})
        number = len([call for call in self.calls if call["path"] == path])
        if (path, number) in self.answers:
            return self.answers[(path, number)]
        if path == PING:
            return response(
                200,
                {"tenant": 7, "name": "Tienda Demo", "sales_source_loaded": self.sales_source_loaded},
            )
        if path == SALES:
            documents = {row["doc_number"] for row in payload["rows"]}
            return response(
                200,
                {
                    "sales_source": payload["source"],
                    "orders": len(documents),
                    "orders_skipped": 0,
                    "orders_revised": 0,
                    "orders_retracted": len(payload["retract"]),
                    "orders_retracted_previous_source": len(payload["previous_source_retract"]),
                    "demand_rows": len(payload["rows"]),
                },
            )
        if path == COMPLETE:
            return response(201, {"log_id": 9})
        return response(200, {"rows": len(payload["rows"])})

    def payloads(self, path):
        return [call["json"] for call in self.calls if call["path"] == path]

    def paths(self):
        return [call["path"] for call in self.calls]


class KalkuSyncCase(KalkuFixtureCase):
    def setUp(self):
        super().setUp()
        self.params = self.env["ir.config_parameter"].sudo()
        self.configure(
            url=BASE,
            token="plain-token",
            sync_sales="True",
            sync_stock="False",
            sync_supplier_terms="False",
            sales_source="orders",
        )

    def configure(self, **values):
        for key, value in values.items():
            self.params.set_param("kalku.%s" % key, value)

    def sync(self, fake, method="run", **kwargs):
        with patch("requests.Session.request", side_effect=fake), patch(
            "time.sleep"
        ) as self.sleep:
            return getattr(self.env["kalku.sync"], method)(**kwargs)

    def set_watermark(self, value, source="orders"):
        self.params.set_param(WATERMARK, value)
        self.params.set_param(WATERMARK_SOURCE, source)

    def last_log(self):
        return self.env["kalku.sync.log"].search([], limit=1)


@tagged("post_install", "-at_install")
class TestKalkuSync(KalkuSyncCase):
    def _second_order(self):
        return self._order(
            self.company_a,
            "S00044",
            self.customer,
            self.warehouse_a,
            "2026-10-08 15:00:00",
            [(self.camiseta, 4, 25000)],
        )

    @requires_sale_stock
    def test_full_run_posts_chunks_and_completes(self):
        self._second_order()
        self.configure(sync_stock="True", sync_supplier_terms="True")
        fake = FakeKalku()
        with patch.object(kalku_sync, "CHUNK_DOCUMENTS", 1), patch.object(
            kalku_sync, "CHUNK_PRODUCTS", 1
        ):
            log = self.sync(fake)

        sales = fake.payloads(SALES)
        # Two confirmed documents with rows, one document per request.
        self.assertEqual(len(sales), 2)
        self.assertEqual(sales[0]["retract"], ["S00043"])
        self.assertEqual(sales[1]["retract"], [])
        for payload in sales:
            self.assertEqual(payload["source"], "orders")
            self.assertEqual(payload["previous_source_retract"], [])
            self.assertNotIn("previous_source", payload)

        # Stock: one request per product. Supplier terms: both rows of CAM-001 together.
        self.assertEqual([len(p["rows"]) for p in fake.payloads(INVENTORY)], [1, 1])
        self.assertEqual([len(p["rows"]) for p in fake.payloads(SUPPLIER_TERMS)], [2])
        self.assertEqual(
            fake.paths(),
            [PING, SALES, SALES, INVENTORY, INVENTORY, SUPPLIER_TERMS, COMPLETE],
        )

        complete = fake.payloads(COMPLETE)[0]
        self.assertEqual(complete["status"], "completed")
        self.assertEqual(complete["sync_type"], "full")
        self.assertEqual(complete["datasets"]["sales"]["orders"], 2)
        self.assertEqual(complete["datasets"]["sales"]["demand_rows"], 3)
        self.assertEqual(complete["datasets"]["sales"]["orders_retracted"], 1)
        self.assertEqual(complete["datasets"]["sales"]["sales_source"], "orders")
        self.assertEqual(complete["datasets"]["inventory"], {"rows": 2})
        self.assertEqual(complete["datasets"]["supplier_terms"], {"rows": 2})
        # Sales lines (1 + 1), stock products (1 + 1), supplier rows (1 + 0).
        self.assertEqual(
            complete["datasets"]["skipped"],
            {"no_default_code": 3, "service": 2, "no_warehouse_code": 0},
        )
        self.assertNotIn("error", complete)

        self.assertEqual(log.state, "done")
        self.assertEqual(log.sync_type, "full")
        self.assertEqual(log.dataset, "all")
        self.assertEqual(log.documents_read, 3)
        self.assertEqual(log.rows_sent, 7)
        self.assertEqual(log.skipped_no_default_code, 3)
        self.assertTrue(log.finished_at)
        self.assertEqual(json.loads(log.kalku_response), complete["datasets"])
        started = fields.Datetime.to_string(log.started_at)
        self.assertEqual(self.params.get_param(WATERMARK), started)
        self.assertEqual(self.params.get_param(WATERMARK_SOURCE), "orders")
        self.assertEqual(complete["started_at"], log.started_at.isoformat() + "+00:00")

    @requires_sale_stock
    def test_document_never_split_across_chunks(self):
        self._second_order()
        fake = FakeKalku()
        with patch.object(kalku_sync, "CHUNK_DOCUMENTS", 1):
            self.sync(fake)
        sales = fake.payloads(SALES)
        for payload in sales:
            self.assertEqual(len({row["doc_number"] for row in payload["rows"]}), 1)
        s00042 = [p for p in sales if p["rows"][0]["doc_number"] == "S00042"]
        self.assertEqual(len(s00042), 1)
        self.assertEqual(
            sorted(row["product_key"] for row in s00042[0]["rows"]), ["CAM-001", "CAM-002"]
        )

    @requires_sale_stock
    def test_source_switch_sends_previous_source_retract(self):
        self.set_watermark("2026-11-01 00:00:00")
        fake = FakeKalku(sales_source_loaded="invoices")
        log = self.sync(fake)

        sales = fake.payloads(SALES)
        self.assertEqual(
            sales[0],
            {
                "source": "orders",
                "previous_source": "invoices",
                "rows": [],
                "retract": [],
                "previous_source_retract": ["INV/2026/0007", "RINV/2026/0002"],
            },
        )
        self.assertEqual(len(sales), 2)
        self.assertEqual(sales[1]["previous_source_retract"], [])
        self.assertNotIn("previous_source", sales[1])
        self.assertEqual({row["doc_number"] for row in sales[1]["rows"]}, {"S00042"})
        # A source switch reloads the new source from the start.
        self.assertEqual(log.sync_type, "full")
        self.assertEqual(fake.payloads(COMPLETE)[0]["sync_type"], "full")
        self.assertEqual(log.state, "done")

    @requires_sale_stock
    def test_previous_source_retract_is_sliced(self):
        fake = FakeKalku(sales_source_loaded="invoices")
        with patch.object(kalku_sync, "CHUNK_NUMBERS", 1):
            self.sync(fake)
        sales = fake.payloads(SALES)
        self.assertEqual(
            [p["previous_source_retract"] for p in sales],
            [["INV/2026/0007"], ["RINV/2026/0002"], []],
        )
        self.assertEqual([len(p["rows"]) for p in sales], [0, 0, 2])

    @requires_sale_stock
    def test_same_source_sends_no_previous_source(self):
        fake = FakeKalku(sales_source_loaded="orders")
        self.sync(fake)
        sales = fake.payloads(SALES)
        self.assertEqual(len(sales), 1)
        self.assertEqual(sales[0]["previous_source_retract"], [])

    @requires_sale_stock
    def test_auth_error_stops_and_keeps_watermark(self):
        self._second_order()
        self.configure(sync_stock="True", sync_supplier_terms="True")
        self.set_watermark("2026-01-01 00:00:00")
        fake = FakeKalku(answers={(SALES, 2): response(401, {"message": INACTIVE_OWNER})})
        with patch.object(kalku_sync, "CHUNK_DOCUMENTS", 1):
            log = self.sync(fake)

        self.assertEqual(log.state, "failed")
        self.assertEqual(log.error, INACTIVE_OWNER)
        self.assertTrue(log.finished_at)
        # Nothing after the refused request, not even the closing call.
        self.assertEqual(fake.paths(), [PING, SALES, SALES])
        self.assertEqual(self.params.get_param(WATERMARK), "2026-01-01 00:00:00")

    @requires_sale_stock
    def test_scope_error_stops_without_complete(self):
        fake = FakeKalku(
            answers={(PING, 1): response(403, {"message": "Insufficient token scope."})}
        )
        log = self.sync(fake)
        self.assertEqual(fake.paths(), [PING])
        self.assertEqual(log.state, "failed")
        self.assertEqual(log.error, "Insufficient token scope.")

    @requires_sale_stock
    def test_other_error_reports_failed_run_and_keeps_watermark(self):
        self.set_watermark("2026-01-01 00:00:00")
        refused = response(
            422,
            {
                "message": "Máximo 500 documentos por envío.",
                "errors": {"rows": ["Máximo 500 documentos por envío."]},
            },
        )
        fake = FakeKalku(answers={(SALES, 1): refused})
        log = self.sync(fake)

        self.assertEqual(fake.paths(), [PING, SALES, COMPLETE])
        complete = fake.payloads(COMPLETE)[0]
        self.assertEqual(complete["status"], "failed")
        self.assertEqual(complete["sync_type"], "incremental")
        self.assertEqual(complete["error"], "Máximo 500 documentos por envío.")
        self.assertEqual(log.state, "failed")
        self.assertEqual(log.error, "Máximo 500 documentos por envío.")
        self.assertIn("Máximo 500 documentos por envío.", log.kalku_response)
        self.assertEqual(self.params.get_param(WATERMARK), "2026-01-01 00:00:00")

    @requires_sale_stock
    def test_429_retries_once(self):
        fake = FakeKalku(
            answers={
                (SALES, 1): response(
                    429, {"message": "Too Many Attempts."}, {"Retry-After": "0"}
                )
            }
        )
        log = self.sync(fake)
        self.assertEqual(log.state, "done")
        self.sleep.assert_called_once_with(0)
        sales = fake.payloads(SALES)
        self.assertEqual(len(sales), 2)
        self.assertEqual(sales[0], sales[1])
        self.assertEqual(fake.paths()[-1], COMPLETE)

    @requires_sale_stock
    def test_incremental_reads_since_watermark_minus_24h(self):
        self.set_watermark("2026-11-05 10:00:00")
        Readers = type(self.env["kalku.readers"])
        with patch.object(
            Readers, "read_sales", autospec=True, side_effect=Readers.read_sales
        ) as read_sales:
            log = self.sync(FakeKalku(sales_source_loaded="orders"))
        _readers, source, since = read_sales.call_args.args
        self.assertEqual(source, "orders")
        self.assertEqual(since, fields.Datetime.from_string("2026-11-04 10:00:00"))
        self.assertEqual(log.sync_type, "incremental")
        self.assertEqual(log.state, "done")

    @requires_sale_stock
    def test_watermark_of_another_source_reads_everything(self):
        # A switch whose rows failed after Kalku already took the new source:
        # ping no longer reports a switch, but the watermark is the old source's.
        self.set_watermark("2026-11-05 10:00:00", source="invoices")
        Readers = type(self.env["kalku.readers"])
        with patch.object(
            Readers, "read_sales", autospec=True, side_effect=Readers.read_sales
        ) as read_sales:
            log = self.sync(FakeKalku(sales_source_loaded="orders"))
        self.assertIsNone(read_sales.call_args.args[2])
        self.assertEqual(log.sync_type, "full")
        self.assertEqual(self.params.get_param(WATERMARK_SOURCE), "orders")

    @requires_sale_stock
    def test_full_sync_type_reads_everything(self):
        self.set_watermark("2026-11-05 10:00:00")
        Readers = type(self.env["kalku.readers"])
        with patch.object(
            Readers, "read_sales", autospec=True, side_effect=Readers.read_sales
        ) as read_sales:
            log = self.sync(FakeKalku(), sync_type="full")
        self.assertIsNone(read_sales.call_args.args[2])
        self.assertEqual(log.sync_type, "full")

    def test_cron_never_raises(self):
        self.configure(sales_source="auto")
        fake = FakeKalku(
            answers={
                (PING, 1): response(500, {"message": "Server Error"}),
                (COMPLETE, 1): response(500, {"message": "Server Error"}),
            }
        )
        self.assertIsNone(self.sync(fake, method="run_scheduled"))
        log = self.last_log()
        self.assertEqual(log.state, "failed")
        self.assertEqual(log.error, "Server Error")
        self.assertEqual(fake.payloads(COMPLETE)[0]["status"], "failed")

    def test_cron_survives_an_unexpected_error(self):
        def broken(method, url, **kwargs):
            raise RuntimeError("boom")

        with self.assertLogs("odoo.addons.kalku_connector.models.kalku_sync", "ERROR"):
            self.assertIsNone(self.sync(broken, method="run_scheduled"))
        log = self.last_log()
        self.assertEqual(log.state, "failed")
        self.assertEqual(log.error, "boom")

    def test_odoo_refusal_fails_the_run_without_a_traceback(self):
        refusal = UserError("The Sales app is not installed: send sales from invoices instead.")
        Readers = type(self.env["kalku.readers"])
        fake = FakeKalku()
        with patch.object(Readers, "read_sales", side_effect=refusal), self.assertNoLogs(
            "odoo.addons.kalku_connector.models.kalku_sync", "ERROR"
        ):
            self.assertIsNone(self.sync(fake, method="run_scheduled"))
        log = self.last_log()
        self.assertEqual(log.state, "failed")
        self.assertEqual(log.error, refusal.args[0])
        self.assertEqual(fake.payloads(COMPLETE)[0]["status"], "failed")

    def test_cron_without_configuration_does_nothing(self):
        self.configure(url="")
        fake = FakeKalku()
        self.assertIsNone(self.sync(fake, method="run_scheduled"))
        self.assertEqual(fake.calls, [])
        self.assertFalse(self.last_log())

    def test_run_without_configuration_is_refused(self):
        self.configure(token="")
        with self.assertRaises(UserError):
            self.sync(FakeKalku())

    def test_cron_is_daily(self):
        cron = self.env.ref("kalku_connector.ir_cron_kalku_sync")
        self.assertEqual(cron.code.strip(), "model.run_scheduled()")
        self.assertEqual((cron.interval_number, cron.interval_type), (1, "days"))
        self.assertEqual(cron.model_id.model, "kalku.sync")


@tagged("post_install", "-at_install")
class TestKalkuSettingsButtons(KalkuSyncCase):
    def _settings(self, **values):
        return self.env["res.config.settings"].create(values)

    def test_test_connection_uses_the_values_on_screen(self):
        self.configure(url="", token="")
        settings = self._settings(kalku_url=" https://app.kalku.co/ ", kalku_token="typed-token")
        fake = FakeKalku()
        with patch("requests.Session.request", side_effect=fake):
            action = settings.action_kalku_test_connection()
        self.assertEqual(fake.calls[0]["kwargs"]["headers"]["Authorization"], "Bearer typed-token")
        self.assertEqual(fake.paths(), [PING])
        self.assertEqual(action["tag"], "display_notification")
        self.assertIn("Tienda Demo", action["params"]["message"])

    def test_test_connection_shows_kalku_refusal(self):
        fake = FakeKalku(
            answers={(PING, 1): response(401, {"message": INACTIVE_OWNER})}
        )
        with patch("requests.Session.request", side_effect=fake), self.assertRaises(
            UserError
        ) as raised:
            self._settings().action_kalku_test_connection()
        self.assertEqual(str(raised.exception), INACTIVE_OWNER)

    @requires_sale_stock
    def test_sync_now_runs_in_the_request_when_small(self):
        fake = FakeKalku()
        with patch("requests.Session.request", side_effect=fake):
            action = self._settings().action_kalku_sync_now()
        self.assertEqual(fake.paths()[-1], COMPLETE)
        self.assertEqual(self.last_log().state, "done")
        self.assertEqual(action["params"]["type"], "success")

    @requires_sale_stock
    def test_sync_now_reports_a_failed_run(self):
        fake = FakeKalku(answers={(PING, 1): response(401, {"message": INACTIVE_OWNER})})
        with patch("requests.Session.request", side_effect=fake):
            action = self._settings().action_kalku_sync_now()
        self.assertEqual(action["params"]["type"], "danger")
        self.assertIn(INACTIVE_OWNER, action["params"]["message"])

    @requires_sale_stock
    def test_sync_now_hands_large_runs_to_the_cron(self):
        cron = self.env.ref("kalku_connector.ir_cron_kalku_sync")
        Trigger = self.env["ir.cron.trigger"].sudo()
        before = Trigger.search_count([("cron_id", "=", cron.id)])
        fake = FakeKalku()
        with patch.object(kalku_sync, "MAX_DOCUMENTS_IN_REQUEST", 1), patch(
            "requests.Session.request", side_effect=fake
        ):
            action = self._settings().action_kalku_sync_now()
        self.assertEqual(fake.calls, [])
        self.assertEqual(Trigger.search_count([("cron_id", "=", cron.id)]), before + 1)
        self.assertEqual(
            action["params"]["message"],
            "Sync started; check Inventory > Configuration > Kalku > Sync log.",
        )


@tagged("post_install", "-at_install")
class TestKalkuClient(TransactionCase):
    def _client(self, *answers, url="https://app.kalku.co/ "):
        calls = []

        def fake(method, url, **kwargs):
            calls.append((method, url, kwargs))
            answer = answers[len(calls) - 1]
            if isinstance(answer, Exception):
                raise answer
            return answer

        # Each call replaces the previous fakes (subTest loops call it again).
        patch.stopall()
        self.calls = calls
        patch("requests.Session.request", side_effect=fake).start()
        self.sleep = patch("time.sleep").start()
        self.addCleanup(patch.stopall)
        return KalkuClient(url, "plain-token")

    def test_request_shape(self):
        client = self._client(response(200, {"name": "Tienda Demo"}))
        self.assertEqual(client.ping(), {"name": "Tienda Demo"})
        method, url, kwargs = self.calls[0]
        self.assertEqual((method, url), ("GET", "https://app.kalku.co/api/connector/ping"))
        self.assertEqual(kwargs["timeout"], 60)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer plain-token")
        self.assertEqual(kwargs["headers"]["Accept"], "application/json")

    def test_endpoints(self):
        client = self._client(*[response(200, {"ok": 1})] * 3, response(201, {"log_id": 3}))
        client.push_sales({"source": "orders"})
        client.push_inventory([{"product_key": "A"}])
        client.push_supplier_terms([{"product_key": "A"}])
        self.assertEqual(client.complete({"status": "completed"}), {"log_id": 3})
        self.assertEqual(
            [(method, url[len(BASE):], kwargs["json"]) for method, url, kwargs in self.calls],
            [
                ("POST", SALES, {"source": "orders"}),
                ("POST", INVENTORY, {"rows": [{"product_key": "A"}]}),
                ("POST", SUPPLIER_TERMS, {"rows": [{"product_key": "A"}]}),
                ("POST", COMPLETE, {"status": "completed"}),
            ],
        )

    def test_401_and_403_raise_auth_error(self):
        for status, message in ((401, INACTIVE_OWNER), (403, "Insufficient token scope.")):
            with self.subTest(status=status):
                client = self._client(response(status, {"message": message}))
                with self.assertRaises(KalkuAuthError) as raised:
                    client.ping()
                self.assertEqual(raised.exception.message, message)
                self.assertEqual(raised.exception.status, status)

    def test_other_status_raises_kalku_error_with_its_message(self):
        client = self._client(response(422, {"message": "previous_source es obligatorio.", "errors": {"x": ["y"]}}))
        with self.assertRaises(KalkuError) as raised:
            client.push_sales({})
        self.assertNotIsInstance(raised.exception, KalkuAuthError)
        self.assertEqual(raised.exception.status, 422)
        self.assertEqual(raised.exception.message, "previous_source es obligatorio.")
        self.assertEqual(raised.exception.errors, {"x": ["y"]})

    def test_non_json_error_body(self):
        bad_gateway = requests.Response()
        bad_gateway.status_code = 502
        bad_gateway._content = b"<html>Bad Gateway</html>"
        client = self._client(bad_gateway)
        with self.assertRaises(KalkuError) as raised:
            client.ping()
        self.assertEqual(raised.exception.status, 502)
        self.assertEqual(raised.exception.message, "Kalku answered HTTP 502.")

    def test_network_error_raises_kalku_error(self):
        client = self._client(requests.Timeout("read timed out"))
        with self.assertRaises(KalkuError) as raised:
            client.ping()
        self.assertIsNone(raised.exception.status)
        self.assertIn("read timed out", raised.exception.message)

    def test_429_waits_retry_after_capped_and_retries_once(self):
        for header, waited in (({"Retry-After": "500"}, 120), ({}, 30), ({"Retry-After": "4"}, 4)):
            with self.subTest(header=header):
                client = self._client(
                    response(429, {"message": "Too Many Attempts."}, header),
                    response(200, {"name": "Tienda Demo"}),
                )
                self.assertEqual(client.ping(), {"name": "Tienda Demo"})
                self.sleep.assert_called_once_with(waited)

    def test_second_429_raises(self):
        client = self._client(
            response(429, {"message": "Too Many Attempts."}, {"Retry-After": "0"}),
            response(429, {"message": "Too Many Attempts."}, {"Retry-After": "0"}),
        )
        with self.assertRaises(KalkuError) as raised:
            client.ping()
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(len(self.calls), 2)
