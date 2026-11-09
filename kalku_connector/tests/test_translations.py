from odoo.tests.common import TransactionCase


class TestKalkuTranslations(TransactionCase):
    def test_spanish_terms_are_loaded(self):
        self.env["res.lang"]._activate_lang("es_ES")
        self.env["ir.module.module"]._load_module_terms(
            ["kalku_connector"], ["es_ES"], overwrite=True
        )
        model = self.env["ir.model"]._get("kalku.sync.log").with_context(lang="es_ES")
        self.assertEqual(model.name, "Registro de sincronización de Kalku")
        field = self.env["kalku.sync.log"].with_context(lang="es_ES")._fields[
            "skipped_no_default_code"
        ]
        self.assertEqual(
            field._description_string(self.env(context={"lang": "es_ES"})),
            "Omitidos: sin referencia interna",
        )
