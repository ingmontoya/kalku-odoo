from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase


class TestKalkuSettings(TransactionCase):
    def _save(self, **values):
        settings = self.env["res.config.settings"].create(values)
        settings.execute()
        return settings

    def _param(self, key):
        return self.env["ir.config_parameter"].sudo().get_param(key)

    def test_save_stores_parameters(self):
        self._save(
            kalku_url="https://app.kalku.co",
            kalku_token="secret-token",
            kalku_sync_sales=True,
            kalku_sync_stock=True,
            kalku_sync_supplier_terms=False,
            kalku_sales_source="orders",
        )
        self.assertEqual(self._param("kalku.url"), "https://app.kalku.co")
        self.assertEqual(self._param("kalku.token"), "secret-token")
        self.assertEqual(self._param("kalku.sync_sales"), "True")
        self.assertEqual(self._param("kalku.sync_stock"), "True")
        self.assertEqual(self._param("kalku.sync_supplier_terms"), "False")
        self.assertEqual(self._param("kalku.sales_source"), "orders")

    def test_sales_source_defaults_to_auto(self):
        settings = self.env["res.config.settings"].create({})
        self.assertEqual(settings.kalku_sales_source, "auto")

    def test_token_is_never_read_back(self):
        self._save(kalku_url="https://app.kalku.co", kalku_token="secret-token")
        values = self.env["res.config.settings"].get_values()
        self.assertEqual(values["kalku_token"], "")

    def test_empty_token_keeps_stored_token(self):
        self._save(kalku_url="https://app.kalku.co", kalku_token="secret-token")
        self._save(kalku_url="https://app.kalku.co", kalku_token="")
        self.assertEqual(self._param("kalku.token"), "secret-token")

    def test_url_validation(self):
        for url in (
            "http://example.com",
            "http://localhost.evil.com",
            "http://localhost@evil.com",
            "ftp://app.kalku.co",
            "https://",
        ):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                self._save(kalku_url=url)

    def test_allowed_urls(self):
        for url in (
            "HTTPS://app.kalku.co",
            "https://app.kalku.co",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "http://host.docker.internal:8000",
        ):
            with self.subTest(url=url):
                self._save(kalku_url=url)
                self.assertEqual(self._param("kalku.url"), url)

    def test_sync_log_can_be_created(self):
        log = self.env["kalku.sync.log"].create(
            {"sync_type": "full", "dataset": "sales", "state": "done"}
        )
        self.assertEqual(log.state, "done")
        self.assertEqual(log.rows_sent, 0)

    def test_stock_manager_reads_log_but_cannot_write(self):
        log = self.env["kalku.sync.log"].create(
            {"sync_type": "full", "dataset": "sales", "state": "done"}
        )
        user = self.env["res.users"].create(
            {
                "name": "Stock manager",
                "login": "kalku_stock_manager",
                "groups_id": [(6, 0, [self.env.ref("stock.group_stock_manager").id])],
            }
        )
        log_as_user = log.with_user(user)
        self.assertEqual(log_as_user.state, "done")
        with self.assertRaises(AccessError):
            log_as_user.write({"error": "x"})
        with self.assertRaises(AccessError):
            self.env["kalku.sync.log"].with_user(user).create(
                {"sync_type": "full", "dataset": "sales"}
            )
