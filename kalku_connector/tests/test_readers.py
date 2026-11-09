from datetime import date, timedelta

from odoo import fields

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import KalkuFixtureCase, load_fixture, requires_sale_stock


def _sales_order(rows):
    return sorted(rows, key=lambda row: (row["doc_number"], row["product_key"]))


@tagged("post_install", "-at_install")
class TestKalkuReaders(KalkuFixtureCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fixture = load_fixture()

    def _assert_sales_match(self, result, expected):
        self.assertEqual(result["source"], expected["source"])
        self.assertEqual(_sales_order(result["rows"]), _sales_order(expected["rows"]))
        self.assertEqual(sorted(result["retract"]), sorted(expected["retract"]))
        self.assertEqual(result["skipped"], expected["skipped"])

    @requires_sale_stock
    def test_sales_orders_match_fixture(self):
        # The order carries tax, so matching the fixture proves amount is untaxed.
        self.assertTrue(self.order_42.amount_tax)
        result = self.readers().read_sales("orders", None)
        self._assert_sales_match(result, self.fixture["sales_orders"])
        self.assertEqual(sorted(result["documents"]), ["S00042", "S00043"])

    @requires_sale_stock
    def test_sales_invoices_match_fixture(self):
        result = self.readers().read_sales("invoices", None)
        self._assert_sales_match(result, self.fixture["sales_invoices"])
        refund = [row for row in result["rows"] if row["doc_number"] == "RINV/2026/0002"]
        self.assertEqual([row["quantity"] for row in refund], [-1])
        self.assertEqual(sorted(result["documents"]), ["INV/2026/0007", "RINV/2026/0002"])

    def test_stock_match_fixture(self):
        expected = self.fixture["inventory"]
        result = self.readers().read_stock(date(2026, 11, 8))
        self.assertEqual(
            sorted(result["rows"], key=lambda row: row["product_key"]), expected["rows"]
        )
        self.assertEqual(result["skipped"], expected["skipped"])

    def test_supplier_terms_match_fixture(self):
        expected = self.fixture["supplier_terms"]
        result = self.readers().read_supplier_terms()
        self.assertEqual(
            sorted(result["rows"], key=lambda row: (row["product_key"], row["supplier_key"])),
            expected["rows"],
        )
        self.assertEqual(result["skipped"], expected["skipped"])

    @requires_sale_stock
    def test_default_code_is_stripped(self):
        product = self.env["product.product"].create(
            {"name": "Pantalón", "default_code": " CAM-009 ", "type": "consu", "is_storable": True}
        )
        # The ORM keeps the spaces (only the web client trims), so the reader must strip.
        self.assertEqual(product.default_code, " CAM-009 ")
        self._order(
            self.company_a,
            "S00044",
            self.customer,
            self.warehouse_a,
            "2026-10-07 15:00:00",
            [(product, 2, 30000)],
        )
        rows = self.readers().read_sales("orders", None)["rows"]
        self.assertEqual(
            [row["product_key"] for row in rows if row["doc_number"] == "S00044"], ["CAM-009"]
        )
        stock_keys = [row["product_key"] for row in self.readers().read_stock(date(2026, 11, 8))["rows"]]
        self.assertIn("CAM-009", stock_keys)

    def test_other_company_excluded(self):
        readers = self.readers()
        readers_b = readers.with_company(self.company_b)
        stock = {row["product_key"]: row["quantity"] for row in readers.read_stock(date(2026, 11, 8))["rows"]}
        self.assertEqual(stock["CAM-001"], 7)
        stock_b = {row["product_key"]: row["quantity"] for row in readers_b.read_stock(date(2026, 11, 8))["rows"]}
        self.assertEqual(stock_b["CAM-001"], 100)
        suppliers = [row["supplier_key"] for row in readers.read_supplier_terms()["rows"]]
        self.assertNotIn("811000001", suppliers)
        suppliers_b = [row["supplier_key"] for row in readers_b.read_supplier_terms()["rows"]]
        self.assertEqual(suppliers_b, ["811000001"])
        if not self.has_sales:
            return
        orders = readers.read_sales("orders", None)
        self.assertNotIn("S09001", [row["doc_number"] for row in orders["rows"]])
        self.assertNotIn("S09002", orders["retract"])
        self.assertNotIn("S09001", orders["documents"])
        self.assertNotIn("901000001", [row["customer_key"] for row in orders["rows"]])
        # Read from company B, the same database answers with B's documents only.
        orders_b = readers_b.read_sales("orders", None)
        self.assertEqual(sorted(orders_b["documents"]), ["S09001", "S09002"])
        self.assertEqual(orders_b["retract"], ["S09002"])

    @requires_sale_stock
    def test_incremental_window(self):
        # Records created in one transaction share write_date; set them by hand.
        since = fields.Datetime.now() - timedelta(days=1)
        self.env.cr.execute(
            "UPDATE sale_order SET write_date = %s WHERE id = %s",
            [since - timedelta(seconds=1), self.order_42.id],
        )
        self.env.cr.execute(
            "UPDATE sale_order SET write_date = %s WHERE id = %s",
            [since, self.order_43.id],
        )
        self.env.invalidate_all()
        result = self.readers().read_sales("orders", since)
        self.assertNotIn("S00042", result["documents"])
        self.assertNotIn("S00042", [row["doc_number"] for row in result["rows"]])
        # write_date == since is inside the window.
        self.assertEqual(result["retract"], ["S00043"])
        full = self.readers().read_sales("orders", None)
        self.assertIn("S00042", full["documents"])

    def test_resolve_source(self):
        readers = self.readers()
        self.assertEqual(readers.resolve_source("orders"), "orders")
        self.assertEqual(readers.resolve_source("invoices"), "invoices")
        expected = "orders" if "sale.order" in self.env else "invoices"
        self.assertEqual(readers.resolve_source("auto"), expected)
        with self.assertRaises(UserError):
            readers.resolve_source("quotes")

    def test_unknown_source_is_refused(self):
        with self.assertRaises(UserError):
            self.readers().read_sales("quotes", None)

    @requires_sale_stock
    def test_variant_name_carries_its_attributes(self):
        size = self.env["product.attribute"].create(
            {"name": "Talla", "value_ids": [(0, 0, {"name": "S"}), (0, 0, {"name": "M"})]}
        )
        template = self.env["product.template"].create(
            {
                "name": "Polo",
                "type": "consu",
                "is_storable": True,
                "attribute_line_ids": [
                    (0, 0, {"attribute_id": size.id, "value_ids": [(6, 0, size.value_ids.ids)]})
                ],
            }
        )
        variants = {v.product_template_attribute_value_ids.name: v for v in template.product_variant_ids}
        variants["S"].default_code = "POL-S"
        variants["M"].default_code = "POL-M"
        self._order(
            self.company_a,
            "S00045",
            self.customer,
            self.warehouse_a,
            "2026-10-08 15:00:00",
            [(variants["S"], 1, 20000), (variants["M"], 2, 20000)],
        )
        rows = [row for row in self.readers().read_sales("orders", None)["rows"] if row["doc_number"] == "S00045"]
        self.assertEqual(
            sorted((row["product_key"], row["product_name"]) for row in rows),
            [("POL-M", "Polo (M)"), ("POL-S", "Polo (S)")],
        )

    def _supplier_line(self, partner, product, **values):
        return self.env["product.supplierinfo"].create(
            dict({"partner_id": partner.id, "product_tmpl_id": product.product_tmpl_id.id}, **values)
        )

    def _supplier_rows(self, product_key):
        rows = self.readers().read_supplier_terms()["rows"]
        return [row for row in rows if row["product_key"] == product_key]

    def test_one_row_per_product_and_supplier(self):
        product = self.env["product.product"].create(
            {"name": "Chaqueta", "default_code": "CAM-010", "type": "consu", "is_storable": True}
        )
        self._supplier_line(self.supplier_one, product, sequence=1, min_qty=0, price=100, delay=5)
        self._supplier_line(self.supplier_one, product, sequence=3, min_qty=100, price=90, delay=7)
        # Another contact of the same company is the same supplier.
        contact = self.env["res.partner"].create(
            {"name": "Ventas Proveedor Uno", "parent_id": self.supplier_one.id}
        )
        self._supplier_line(contact, product, sequence=4, min_qty=1, price=80, delay=2)
        rows = self._supplier_rows("CAM-010")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["supplier_key"], "800111222")
        self.assertTrue(rows[0]["is_preferred"])
        self.assertEqual(rows[0]["lead_time_days"], 5)
        self.assertEqual(rows[0]["unit_price"], 100)
        self.assertIsNone(rows[0]["min_order_qty"])

    def test_supplier_lines_out_of_dates_are_ignored(self):
        product = self.env["product.product"].create(
            {"name": "Chaleco", "default_code": "CAM-011", "type": "consu", "is_storable": True}
        )
        today = fields.Date.context_today(self.env.user)
        self._supplier_line(self.supplier_one, product, sequence=1, delay=1, date_end=today - timedelta(days=1))
        self._supplier_line(self.supplier_two, product, sequence=2, delay=2, date_start=today + timedelta(days=1))
        current = self.env["res.partner"].create({"name": "Proveedor Vigente", "vat": "800333444"})
        self._supplier_line(current, product, sequence=3, delay=3, date_start=today, date_end=today)
        rows = self._supplier_rows("CAM-011")
        self.assertEqual([row["supplier_key"] for row in rows], ["800333444"])
        self.assertTrue(rows[0]["is_preferred"])

    def test_archived_suppliers_are_ignored(self):
        product = self.env["product.product"].create(
            {"name": "Bufanda", "default_code": "CAM-012", "type": "consu", "is_storable": True}
        )
        archived = self.env["res.partner"].create({"name": "Proveedor Archivado", "vat": "800555666"})
        archived_company = self.env["res.partner"].create(
            {"name": "Empresa Archivada", "vat": "800777888", "is_company": True}
        )
        active_contact = self.env["res.partner"].create(
            {"name": "Contacto Activo", "parent_id": archived_company.id}
        )
        self._supplier_line(archived, product, sequence=1, delay=1)
        self._supplier_line(active_contact, product, sequence=2, delay=2)
        self._supplier_line(self.supplier_one, product, sequence=3, delay=3)
        archived.active = False
        archived_company.active = False
        self.assertTrue(active_contact.active)
        rows = self._supplier_rows("CAM-012")
        self.assertEqual([row["supplier_key"] for row in rows], ["800111222"])
        self.assertTrue(rows[0]["is_preferred"])

    def test_stock_ignores_goods_without_stock_tracking(self):
        self.env["product.product"].create(
            {"name": "Bolsa", "type": "consu", "is_storable": False}
        )
        self.env["product.product"].create(
            {"name": "Etiqueta", "default_code": "ETQ-001", "type": "consu", "is_storable": False}
        )
        result = self.readers().read_stock(date(2026, 11, 8))
        self.assertEqual(result["skipped"], self.fixture["inventory"]["skipped"])
        self.assertNotIn("ETQ-001", [row["product_key"] for row in result["rows"]])

    @requires_sale_stock
    def test_count_sales_matches_read_sales(self):
        for source in ("orders", "invoices"):
            with self.subTest(source=source):
                self.assertEqual(
                    self.readers().count_sales(source, None),
                    len(self.readers().read_sales(source, None)["documents"]),
                )
