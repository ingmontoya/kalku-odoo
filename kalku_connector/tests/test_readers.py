from datetime import date, datetime

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
        # Records created in one transaction share write_date; age S00042 by hand.
        self.env.cr.execute(
            "UPDATE sale_order SET write_date = %s WHERE id = %s",
            ["2026-10-03 16:00:00", self.order_42.id],
        )
        self.env.invalidate_all()
        result = self.readers().read_sales("orders", datetime(2026, 11, 1))
        self.assertNotIn("S00042", result["documents"])
        self.assertNotIn("S00042", [row["doc_number"] for row in result["rows"]])
        self.assertEqual(result["retract"], ["S00043"])
        full = self.readers().read_sales("orders", None)
        self.assertIn("S00042", full["documents"])

    def test_resolve_source(self):
        readers = self.readers()
        self.assertEqual(readers.resolve_source("orders"), "orders")
        self.assertEqual(readers.resolve_source("invoices"), "invoices")
        expected = "orders" if "sale.order" in self.env else "invoices"
        self.assertEqual(readers.resolve_source("auto"), expected)

    def test_unknown_source_is_refused(self):
        with self.assertRaises(UserError):
            self.readers().read_sales("quotes", None)
