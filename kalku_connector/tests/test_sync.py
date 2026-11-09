import json
from datetime import timedelta
from unittest.mock import patch

import requests
from requests.structures import CaseInsensitiveDict

from odoo import SUPERUSER_ID, Command, api, fields
from odoo.api import call_kw
from odoo.exceptions import AccessError, UserError
from odoo.service.model import get_public_method
from odoo.sql_db import db_connect
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
PENDING = "kalku.pending_previous_source.sales"
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
        # Log rows are written on a cursor of their own; in test mode that
        # cursor shares this test's transaction, so the test can read them.
        self.registry.enter_test_mode(self.cr)
        self.addCleanup(self.registry.leave_test_mode)
        self.params = self.env["ir.config_parameter"].sudo()
        self.configure(
            url=BASE,
            token="plain-token",
            company_id=str(self.company_a.id),
            sync_sales="True",
            sync_stock="False",
            sync_supplier_terms="False",
            sales_source="orders",
        )

    def configure(self, **values):
        for key, value in values.items():
            self.params.set_param("kalku.%s" % key, value)

    def sync(self, fake, method="_run", **kwargs):
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

        self.assertEqual(log["state"], "done")
        self.assertEqual(log["sync_type"], "full")
        self.assertEqual(log["dataset"], "all")
        self.assertEqual(log["documents_read"], 3)
        self.assertEqual(log["rows_sent"], 7)
        self.assertEqual(log["skipped_no_default_code"], 3)
        self.assertTrue(log["finished_at"])
        self.assertEqual(json.loads(log["kalku_response"]), complete["datasets"])
        started = fields.Datetime.to_string(log["started_at"])
        self.assertEqual(self.params.get_param(WATERMARK), started)
        self.assertEqual(self.params.get_param(WATERMARK_SOURCE), "orders")
        self.assertEqual(complete["started_at"], log["started_at"].isoformat() + "+00:00")

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
        self.assertEqual(log["sync_type"], "full")
        self.assertEqual(fake.payloads(COMPLETE)[0]["sync_type"], "full")
        self.assertEqual(log["state"], "done")

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

        self.assertEqual(log["state"], "failed")
        self.assertEqual(log["error"], INACTIVE_OWNER)
        self.assertTrue(log["finished_at"])
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
        self.assertEqual(log["state"], "failed")
        self.assertEqual(log["error"], "Insufficient token scope.")

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
        self.assertEqual(log["state"], "failed")
        self.assertEqual(log["error"], "Máximo 500 documentos por envío.")
        self.assertIn("Máximo 500 documentos por envío.", log["kalku_response"])
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
        self.assertEqual(log["state"], "done")
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
        self.assertEqual(log["sync_type"], "incremental")
        self.assertEqual(log["state"], "done")

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
        self.assertEqual(log["sync_type"], "full")
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
        self.assertEqual(log["sync_type"], "full")

    @requires_sale_stock
    def test_interrupted_switch_retracts_the_whole_old_list_again(self):
        # The last confirmed run sent invoices; the switch to orders breaks
        # after the first slice of the old list.
        self.set_watermark("2026-11-01 00:00:00", source="invoices")
        first = FakeKalku(
            sales_source_loaded="invoices",
            answers={(SALES, 2): response(500, {"message": "Server Error"})},
        )
        with patch.object(kalku_sync, "CHUNK_NUMBERS", 1):
            log = self.sync(first)
            self.assertEqual(log["state"], "failed")
            self.assertEqual(
                [p["previous_source_retract"] for p in first.payloads(SALES)],
                [["INV/2026/0007"], ["RINV/2026/0002"]],
            )
            # Kalku took the new source with the first slice: its ping says orders.
            second = FakeKalku(sales_source_loaded="orders")
            log = self.sync(second)

        sales = second.payloads(SALES)
        self.assertEqual(
            [(p.get("previous_source"), p["previous_source_retract"]) for p in sales[:2]],
            [("invoices", ["INV/2026/0007"]), ("invoices", ["RINV/2026/0002"])],
        )
        self.assertEqual(len(sales), 3)
        self.assertEqual(sales[2]["previous_source_retract"], [])
        self.assertEqual({row["doc_number"] for row in sales[2]["rows"]}, {"S00042"})
        self.assertEqual((log["state"], log["sync_type"]), ("done", "full"))
        self.assertEqual(self.params.get_param(WATERMARK_SOURCE), "orders")

    @requires_sale_stock
    def test_interrupted_first_switch_is_finished_by_the_next_run(self):
        # A tenant moving from Kalku's pull path (invoices) to the module
        # (orders): nothing was ever confirmed here.
        self.assertFalse(self.params.get_param(WATERMARK_SOURCE))
        first = FakeKalku(
            sales_source_loaded="invoices",
            answers={(SALES, 2): response(500, {"message": "Server Error"})},
        )
        with patch.object(kalku_sync, "CHUNK_NUMBERS", 1):
            log = self.sync(first)
            self.assertEqual(log["state"], "failed")
            self.assertEqual(self.params.get_param(PENDING), "invoices")
            # Kalku took the new source with the first slice.
            second = FakeKalku(sales_source_loaded="orders")
            log = self.sync(second)

        sales = second.payloads(SALES)
        self.assertEqual(
            [(p.get("previous_source"), p["previous_source_retract"]) for p in sales[:2]],
            [("invoices", ["INV/2026/0007"]), ("invoices", ["RINV/2026/0002"])],
        )
        self.assertEqual(len(sales), 3)
        self.assertEqual({row["doc_number"] for row in sales[2]["rows"]}, {"S00042"})
        self.assertEqual((log["state"], log["sync_type"]), ("done", "full"))
        self.assertFalse(self.params.get_param(PENDING))
        self.assertEqual(self.params.get_param(WATERMARK_SOURCE), "orders")

    @requires_sale_stock
    def test_pending_marker_is_kept_until_a_run_confirms(self):
        self.params.set_param(PENDING, "invoices")
        fake = FakeKalku(
            sales_source_loaded="orders",
            answers={(COMPLETE, 1): response(500, {"message": "Server Error"})},
        )
        self.sync(fake)
        self.assertEqual(fake.payloads(SALES)[0]["previous_source"], "invoices")
        self.assertEqual(self.params.get_param(PENDING), "invoices")

    def test_pending_marker_is_read_past_a_stale_cache(self):
        # This process caches "no marker" ...
        self.assertFalse(self.params.get_param(PENDING))
        # ... while another worker commits one without telling this process.
        with patch.object(type(self.registry), "clear_cache", lambda *args, **kwargs: None):
            self.env["kalku.sync"]._commit_param(PENDING, "invoices")
        self.assertFalse(self.params.get_param(PENDING))
        self.assertEqual(self.env["kalku.sync"]._previous_source("orders", {}), "invoices")

    def test_run_returns_its_log_values(self):
        self.configure(sync_sales="False", sync_stock="True")
        result = self.sync(FakeKalku())
        self.assertEqual(set(result), {"id", *kalku_sync.LOG_FIELDS})
        self.assertEqual(result["state"], "done")
        self.assertEqual(result["id"], self.last_log().id)

    @requires_sale_stock
    def test_next_run_closes_a_run_that_died(self):
        dead = self.env["kalku.sync.log"].create({"sync_type": "full", "dataset": "sales"})
        self.assertEqual(dead.state, "running")
        self.sync(FakeKalku(sales_source_loaded="orders"))
        dead.invalidate_recordset()
        self.assertEqual(dead.state, "failed")
        self.assertIn("stopped before finishing", dead.error)
        self.assertTrue(dead.finished_at)

    def test_cron_never_raises(self):
        self.configure(sales_source="auto")
        fake = FakeKalku(
            answers={
                (PING, 1): response(500, {"message": "Server Error"}),
                (COMPLETE, 1): response(500, {"message": "Server Error"}),
            }
        )
        self.assertIsNone(self.sync(fake, method="_run_scheduled"))
        log = self.last_log()
        self.assertEqual(log.state, "failed")
        self.assertEqual(log.error, "Server Error")
        self.assertEqual(fake.payloads(COMPLETE)[0]["status"], "failed")

    def test_cron_survives_an_unexpected_error(self):
        def broken(method, url, **kwargs):
            raise RuntimeError("boom")

        with self.assertLogs("odoo.addons.kalku_connector.models.kalku_sync", "ERROR"):
            self.assertIsNone(self.sync(broken, method="_run_scheduled"))
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
            self.assertIsNone(self.sync(fake, method="_run_scheduled"))
        log = self.last_log()
        self.assertEqual(log.state, "failed")
        self.assertEqual(log.error, refusal.args[0])
        self.assertEqual(fake.payloads(COMPLETE)[0]["status"], "failed")

    def test_cron_logs_other_refusals_as_failures(self):
        # Not the lock: an unknown sales source stops the run before its log.
        self.configure(sales_source="weekly")
        with self.assertLogs("odoo.addons.kalku_connector.models.kalku_sync", "ERROR"):
            self.assertIsNone(self.sync(FakeKalku(), method="_run_scheduled"))

    def test_cron_without_configuration_does_nothing(self):
        self.configure(url="")
        fake = FakeKalku()
        self.assertIsNone(self.sync(fake, method="_run_scheduled"))
        self.assertEqual(fake.calls, [])
        self.assertFalse(self.last_log())

    def test_run_without_configuration_is_refused(self):
        self.configure(token="")
        with self.assertRaises(UserError):
            self.sync(FakeKalku())

    def test_cron_is_daily(self):
        cron = self.env.ref("kalku_connector.ir_cron_kalku_sync")
        self.assertEqual(cron.code.strip(), "model._run_scheduled()")
        self.assertEqual(cron.user_id, self.env.ref("base.user_root"))
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

    def test_company_defaults_to_the_active_one_and_is_stored(self):
        # Post-install: creating a company needs every installed module's defaults.
        self.params.set_param("kalku.company_id", "")
        settings = self._settings()
        self.assertEqual(settings.kalku_company_id, self.env.company)
        settings.write({"kalku_company_id": self.company_b.id})
        settings.execute()
        self.assertEqual(self.params.get_param("kalku.company_id"), str(self.company_b.id))
        self.assertEqual(self._settings().kalku_company_id, self.company_b)

    def _user(self, login, group):
        return self.env["res.users"].create(
            {"name": login, "login": login, "groups_id": [Command.set([self.env.ref(group).id])]}
        )

    def test_only_administrators_reach_the_buttons(self):
        settings = self._settings()
        users = (
            self._user("kalku_internal", "base.group_user"),
            self._user("kalku_portal", "base.group_portal"),
        )
        for user in users:
            for method in ("action_kalku_test_connection", "action_kalku_sync_now"):
                with self.subTest(user=user.login, method=method):
                    fake = FakeKalku()
                    model = self.env["res.config.settings"].with_user(user)
                    with patch("requests.Session.request", side_effect=fake), self.assertRaises(
                        AccessError
                    ):
                        # What /web/dataset/call_button does.
                        get_public_method(model, method)
                        call_kw(model, method, [settings.ids], {})
                    self.assertEqual(fake.calls, [])

    def test_sync_methods_cannot_be_called_remotely(self):
        for name in ("_run", "_run_scheduled", "_runs_in_request", "_test_connection"):
            with self.subTest(name=name), self.assertRaises(AccessError):
                get_public_method(self.env["kalku.sync"], name)

    def test_saved_token_never_goes_to_another_url(self):
        fake = FakeKalku()
        settings = self._settings(kalku_url="https://evil.example")
        with patch("requests.Session.request", side_effect=fake):
            with self.assertRaises(UserError) as raised:
                settings.action_kalku_test_connection()
            self.assertEqual(
                str(raised.exception), "Type the token to test a URL other than the saved one."
            )
            with self.assertRaises(UserError):
                self.env["kalku.sync"]._test_connection(url="https://evil.example")
        self.assertEqual(fake.calls, [])

    def test_test_connection_refuses_plain_http(self):
        fake = FakeKalku()
        with patch("requests.Session.request", side_effect=fake), self.assertRaises(
            UserError
        ) as raised:
            self.env["kalku.sync"]._test_connection(url="http://evil.example", token="typed")
        self.assertEqual(str(raised.exception), "Kalku URL must use https.")
        self.assertEqual(fake.calls, [])

    @requires_sale_stock
    def test_sync_now_sends_the_settings_company_only(self):
        fake = FakeKalku()
        settings = self.env["res.config.settings"].with_company(self.company_b).create({})
        self.assertEqual(settings.kalku_company_id, self.company_a)
        with patch("requests.Session.request", side_effect=fake):
            settings.action_kalku_sync_now()
        sales = fake.payloads(SALES)
        self.assertEqual({row["doc_number"] for p in sales for row in p["rows"]}, {"S00042"})
        self.assertEqual([n for p in sales for n in p["retract"]], ["S00043"])
        self.assertEqual(self.last_log().state, "done")

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

        # A later call (subTest loops) stacks newer fakes over the earlier ones.
        self.calls = calls
        self.startPatcher(patch("requests.Session.request", side_effect=fake))
        self.sleep = self.startPatcher(patch("time.sleep"))
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


class PlainSyncCase(TransactionCase):
    """A sync of the demo stock, outside test mode: log rows are really
    committed by their own cursor, and this test's transaction never sees
    them (repeatable read), so they are read and removed on fresh cursors."""

    def setUp(self):
        super().setUp()
        params = self.env["ir.config_parameter"].sudo()
        for key, value in {
            "url": BASE,
            "token": "plain-token",
            "company_id": str(self.env.company.id),
            "sync_sales": "False",
            "sync_stock": "True",
            "sync_supplier_terms": "False",
        }.items():
            params.set_param("kalku.%s" % key, value)
        with self.registry.cursor() as cr:
            cr.execute("SELECT COALESCE(MAX(id), 0) FROM kalku_sync_log")
            self.last_id = cr.fetchone()[0]
        self.addCleanup(self._delete_committed_logs)

    def _delete_committed_logs(self):
        with self.registry.cursor() as cr:
            cr.execute("DELETE FROM kalku_sync_log WHERE id > %s", [self.last_id])

    def committed_logs(self):
        with self.registry.cursor() as cr:
            Log = api.Environment(cr, SUPERUSER_ID, {})["kalku.sync.log"]
            return Log.search_read(
                [("id", ">", self.last_id)], ["state", "error"], order="id"
            )

    def sync(self, fake, method="_run"):
        with patch("requests.Session.request", side_effect=fake), patch("time.sleep"):
            return getattr(self.env["kalku.sync"], method)()


class WorkerKilled(BaseException):
    """Stands for Odoo killing a worker past its time limit: nothing catches it."""


@tagged("post_install", "-at_install")
class TestKalkuSyncCommittedLog(PlainSyncCase):
    def test_killed_run_keeps_its_log_and_the_next_run_closes_it(self):
        fake = FakeKalku(answers={(COMPLETE, 1): WorkerKilled()})

        def killing(method, url, **kwargs):
            answer = fake(method, url, **kwargs)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        with self.assertRaises(WorkerKilled):
            self.sync(killing)
        self.assertEqual(
            [(log["state"], log["error"]) for log in self.committed_logs()], [("running", False)]
        )

        self.sync(FakeKalku())
        logs = self.committed_logs()
        self.assertEqual([log["state"] for log in logs], ["failed", "done"])
        self.assertIn("stopped before finishing", logs[0]["error"])

    def test_killed_switch_keeps_its_pending_marker(self):
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("kalku.sync_sales", "True")
        params.set_param("kalku.sales_source", "orders")
        self.addCleanup(self._delete_committed_param, PENDING)
        Readers = type(self.env["kalku.readers"])
        old = {
            "source": "invoices",
            "rows": [],
            "retract": [],
            "skipped": {"no_default_code": 0, "service": 0, "no_warehouse_code": 0},
            "documents": ["INV/2026/0007"],
        }
        fake = FakeKalku(sales_source_loaded="invoices", answers={(SALES, 1): WorkerKilled()})

        def killing(method, url, **kwargs):
            answer = fake(method, url, **kwargs)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        with patch.object(Readers, "read_sales", return_value=old), self.assertRaises(
            WorkerKilled
        ):
            self.sync(killing)
        self.assertEqual(fake.payloads(SALES)[0]["previous_source"], "invoices")
        # This test's transaction is rolled back; the marker is already committed.
        with self.registry.cursor() as cr:
            cr.execute("SELECT value FROM ir_config_parameter WHERE key = %s", [PENDING])
            self.assertEqual(cr.fetchall(), [("invoices",)])

    def _delete_committed_param(self, key):
        with self.registry.cursor() as cr:
            cr.execute("DELETE FROM ir_config_parameter WHERE key = %s", [key])

    def test_handled_failure_is_committed(self):
        self.sync(
            FakeKalku(
                answers={
                    (PING, 1): response(500, {"message": "Server Error"}),
                    (COMPLETE, 1): response(500, {"message": "Server Error"}),
                }
            )
        )
        self.assertEqual(
            [(log["state"], log["error"]) for log in self.committed_logs()],
            [("failed", "Server Error")],
        )


@tagged("post_install", "-at_install")
class TestKalkuSyncLock(PlainSyncCase):
    def test_a_second_run_is_refused_while_one_runs(self):
        other = db_connect(self.cr.dbname).cursor()
        self.addCleanup(other.close)
        other.execute("SELECT pg_try_advisory_lock(%s)", [kalku_sync.LOCK_KEY])
        self.assertTrue(other.fetchone()[0])
        self.addCleanup(other.execute, "SELECT pg_advisory_unlock(%s)", [kalku_sync.LOCK_KEY])

        fake = FakeKalku()
        with self.assertRaises(UserError) as raised:
            self.sync(fake)
        self.assertEqual(str(raised.exception), "A Kalku sync is already running.")
        self.assertIsNone(self.sync(fake, method="_run_scheduled"))
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.committed_logs(), [])
