"""Records that reproduce ``fixtures/odoo-mapping.json``.

The JSON file is the contract with Kalku's test suite (copied verbatim from
Kalku's ``tests/Fixtures/connector/odoo-mapping.json``). The records below are
built so the readers produce exactly its rows, plus the records the readers
must leave out: a product without internal reference, a service, and the
documents, quants and supplier rows of a second company.
"""

import json
import os
import unittest

from odoo.tests.common import TransactionCase

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "odoo-mapping.json")

# The fixture's second supplier has no VAT, so its key is "ODOO-<partner id>".
SUPPLIER_WITHOUT_VAT_ID = 77


def load_fixture():
    with open(FIXTURE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def requires_sale_stock(test):
    """Sales readers need ``sale`` and ``sale_stock`` (the order's warehouse).

    The module does not depend on them, so the tests that build sale orders run
    only when the database has them installed.
    """

    def check(self):
        if "sale.order" not in self.env or "warehouse_id" not in self.env["sale.order"]._fields:
            raise unittest.SkipTest("sale_stock is not installed")
        return test(self)

    check.__name__ = test.__name__
    check.__doc__ = test.__doc__
    return check


class KalkuFixtureCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.has_sales = (
            "sale.order" in cls.env and "warehouse_id" in cls.env["sale.order"]._fields
        )
        # Products are shared between companies, so anything already in the
        # database would show up in the stock and supplier snapshots.
        cls.env["product.product"].search([]).write({"active": False})

        cls.company_a = cls.env["res.company"].create({"name": "Kalku Fixture A"})
        cls.env["account.chart.template"].try_loading("generic_coa", cls.company_a)
        cls.company_b = cls.env["res.company"].create({"name": "Kalku Fixture B"})
        cls.env = cls.env["base"].with_company(cls.company_a).env

        cls.warehouse_a = cls._warehouse(cls.company_a, "BAQ")
        cls.warehouse_b = cls._warehouse(cls.company_b, "MED")

        cls._create_products()
        cls._create_partners()
        cls._create_stock()
        cls._create_supplier_terms()
        if cls.has_sales:
            cls._create_orders()
            cls._create_invoices()

    @classmethod
    def _warehouse(cls, company, code):
        warehouse = cls.env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)
        warehouse.code = code
        return warehouse

    @classmethod
    def _create_products(cls):
        Product = cls.env["product.product"]
        cls.camisa = Product.create(
            {"name": "Camisa", "default_code": "CAM-001", "type": "consu", "is_storable": True}
        )
        cls.camiseta = Product.create(
            {"name": "Camiseta", "default_code": "CAM-002", "type": "consu", "is_storable": True}
        )
        cls.no_code = Product.create(
            {"name": "Producto sin referencia", "type": "consu", "is_storable": True}
        )
        cls.service = Product.create(
            {"name": "Servicio de envío", "default_code": "SRV-001", "type": "service"}
        )

    @classmethod
    def _create_partners(cls):
        Partner = cls.env["res.partner"]
        cls.customer = Partner.create(
            {"name": "Cliente SAS", "vat": "900123456", "is_company": True}
        )
        # Orders are placed by a contact; the key comes from its company.
        cls.customer_contact = Partner.create(
            {"name": "Compras Cliente SAS", "parent_id": cls.customer.id}
        )
        cls.supplier_one = Partner.create(
            {"name": "Proveedor Uno SAS", "vat": "800111222", "is_company": True}
        )
        cls.supplier_two = cls._partner_with_id(
            SUPPLIER_WITHOUT_VAT_ID, {"name": "Proveedor Dos", "is_company": True}
        )
        cls.customer_b = Partner.create(
            {"name": "Cliente Empresa B", "vat": "901000001", "is_company": True}
        )
        cls.supplier_b = Partner.create(
            {"name": "Proveedor Empresa B", "vat": "811000001", "is_company": True}
        )

    @classmethod
    def _partner_with_id(cls, partner_id, values):
        cr = cls.env.cr
        cr.execute("SELECT 1 FROM res_partner WHERE id = %s", [partner_id])
        if cr.fetchone():
            raise AssertionError(
                "res.partner %s already exists; the fixture needs that id free" % partner_id
            )
        cr.execute("SELECT setval('res_partner_id_seq', %s, false)", [partner_id])
        try:
            partner = cls.env["res.partner"].create(values)
        finally:
            cr.execute(
                "SELECT setval('res_partner_id_seq', (SELECT MAX(id) FROM res_partner))"
            )
        if partner.id != partner_id:
            raise AssertionError("expected partner id %s, got %s" % (partner_id, partner.id))
        return partner

    @classmethod
    def _create_stock(cls):
        Quant = cls.env["stock.quant"]
        Quant._update_available_quantity(cls.camisa, cls.warehouse_a.lot_stock_id, 7)
        Quant.with_company(cls.company_b)._update_available_quantity(
            cls.camisa, cls.warehouse_b.lot_stock_id, 100
        )

    @classmethod
    def _create_supplier_terms(cls):
        Info = cls.env["product.supplierinfo"]
        Info.create(
            {
                "partner_id": cls.supplier_one.id,
                "product_tmpl_id": cls.camisa.product_tmpl_id.id,
                "sequence": 1,
                "delay": 5,
                "min_qty": 12,
                "price": 4200,
            }
        )
        Info.create(
            {
                "partner_id": cls.supplier_two.id,
                "product_tmpl_id": cls.camisa.product_tmpl_id.id,
                "sequence": 2,
                "delay": 9,
                "min_qty": 0,
                "price": 0,
            }
        )
        Info.create(
            {
                "partner_id": cls.supplier_one.id,
                "product_tmpl_id": cls.no_code.product_tmpl_id.id,
                "sequence": 1,
                "delay": 3,
                "min_qty": 1,
                "price": 1500,
            }
        )
        Info.with_company(cls.company_b).create(
            {
                "partner_id": cls.supplier_b.id,
                "product_tmpl_id": cls.camisa.product_tmpl_id.id,
                "company_id": cls.company_b.id,
                "sequence": 0,
                "delay": 2,
                "min_qty": 1,
                "price": 3900,
            }
        )

    @classmethod
    def _order(cls, company, name, partner, warehouse, date_order, lines, confirm=True):
        Order = cls.env["sale.order"].with_company(company)
        order = Order.create(
            {
                "name": name,
                "company_id": company.id,
                "partner_id": partner.id,
                "warehouse_id": warehouse.id,
                "order_line": [
                    (0, 0, {"product_id": product.id, "product_uom_qty": qty, "price_unit": price})
                    for product, qty, price in lines
                ],
            }
        )
        if confirm:
            order.action_confirm()
        else:
            order.action_cancel()
        # Confirming stamps date_order with now(); put the business date back.
        order.date_order = date_order
        return order

    @classmethod
    def _create_orders(cls):
        cls.order_42 = cls._order(
            cls.company_a,
            "S00042",
            cls.customer_contact,
            cls.warehouse_a,
            "2026-10-03 15:00:00",
            [
                (cls.camisa, 3, 10000),
                (cls.camiseta, 1, 25000),
                (cls.service, 1, 8000),
                (cls.no_code, 2, 3000),
            ],
        )
        cls.order_43 = cls._order(
            cls.company_a,
            "S00043",
            cls.customer,
            cls.warehouse_a,
            "2026-10-04 15:00:00",
            [(cls.camisa, 5, 10000)],
            confirm=False,
        )
        cls.order_b = cls._order(
            cls.company_b,
            "S09001",
            cls.customer_b,
            cls.warehouse_b,
            "2026-10-05 15:00:00",
            [(cls.camisa, 4, 10000)],
        )
        cls.order_b_cancelled = cls._order(
            cls.company_b,
            "S09002",
            cls.customer_b,
            cls.warehouse_b,
            "2026-10-06 15:00:00",
            [(cls.camisa, 1, 10000)],
            confirm=False,
        )

    @classmethod
    def _invoice(cls, move_type, name, invoice_date, lines):
        move = cls.env["account.move"].create(
            {
                "move_type": move_type,
                "name": name,
                "partner_id": cls.customer_contact.id,
                "invoice_date": invoice_date,
                "invoice_line_ids": [
                    (
                        0,
                        0,
                        {
                            "product_id": product.id,
                            "quantity": qty,
                            "price_unit": price,
                            "sale_line_ids": [(6, 0, sale_lines.ids)],
                        },
                    )
                    for product, qty, price, sale_lines in lines
                ],
            }
        )
        move.action_post()
        return move

    @classmethod
    def _create_invoices(cls):
        lines_42 = {line.product_id: line for line in cls.order_42.order_line}
        cls.invoice = cls._invoice(
            "out_invoice",
            "INV/2026/0007",
            "2026-10-15",
            [
                (cls.camisa, 2, 10000, lines_42[cls.camisa]),
                (cls.service, 1, 8000, lines_42[cls.service]),
                (cls.no_code, 2, 3000, lines_42[cls.no_code]),
            ],
        )
        cls.refund = cls._invoice(
            "out_refund",
            "RINV/2026/0002",
            "2026-10-20",
            [(cls.camisa, 1, 10000, lines_42[cls.camisa])],
        )

    def readers(self):
        return self.env["kalku.readers"]
