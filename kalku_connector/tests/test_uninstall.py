from odoo.tests.common import TransactionCase


class TestKalkuUninstall(TransactionCase):
    def test_uninstall_removes_every_kalku_parameter(self):
        # Imported here: a missing hook fails this test, not the whole suite.
        from odoo.addons.kalku_connector import uninstall_hook

        params = self.env["ir.config_parameter"].sudo()
        for key in (
            "kalku.url",
            "kalku.token",
            "kalku.company_id",
            "kalku.sync_sales",
            "kalku.last_sync_at.sales",
            "kalku.last_sync_source.sales",
            "kalku.pending_previous_source.sales",
        ):
            params.set_param(key, "x")
        params.set_param("kalkuother.key", "kept")

        uninstall_hook(self.env)

        self.assertEqual(params.search_count([("key", "=like", "kalku.%")]), 0)
        self.assertEqual(params.get_param("kalkuother.key"), "kept")
        self.assertTrue(params.get_param("web.base.url"))
