from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestKalkuSettings(TransactionCase):
    def _save(self, **values):
        settings = self.env["res.config.settings"].create(values)
        settings.execute()
        return settings

    def test_save_stores_parameters(self):
        self._save(
            kalku_url="https://app.kalku.co",
            kalku_token="secret-token",
            kalku_sync_sales=True,
            kalku_sync_stock=True,
            kalku_sync_supplier_terms=False,
            kalku_sales_source="orders",
        )
        params = self.env["ir.config_parameter"].sudo()
        self.assertEqual(params.get_param("kalku.url"), "https://app.kalku.co")
        self.assertEqual(params.get_param("kalku.token"), "secret-token")
        self.assertEqual(params.get_param("kalku.sync_sales"), "True")
        self.assertEqual(params.get_param("kalku.sync_stock"), "True")
        self.assertEqual(params.get_param("kalku.sync_supplier_terms"), "False")
        self.assertEqual(params.get_param("kalku.sales_source"), "orders")

    def test_sales_source_defaults_to_auto(self):
        settings = self.env["res.config.settings"].create({})
        self.assertEqual(settings.kalku_sales_source, "auto")

    def test_http_url_is_refused(self):
        with self.assertRaises(ValidationError):
            self._save(kalku_url="http://example.com")

    def test_local_http_urls_are_allowed(self):
        for url in ("http://localhost:8000", "http://host.docker.internal:8000"):
            self._save(kalku_url=url)
            self.assertEqual(
                self.env["ir.config_parameter"].sudo().get_param("kalku.url"), url
            )

    def test_sync_log_can_be_created(self):
        log = self.env["kalku.sync.log"].create(
            {"sync_type": "full", "dataset": "sales", "state": "done"}
        )
        self.assertEqual(log.state, "done")
        self.assertEqual(log.rows_sent, 0)
