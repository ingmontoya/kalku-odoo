"""Turn Odoo records into the canonical rows Kalku's loaders take.

Everything is read for ``self.env.company`` only. The row keys and values are
pinned by ``tests/fixtures/odoo-mapping.json``, shared with Kalku's test suite.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .compat import CONFIRMED_ORDER_STATES, is_service, is_storable

SALES_SOURCES = ("orders", "invoices")

# Kalku's limits (ConnectorSalesRequest, ConnectorSupplierTermsRequest): one
# value over them makes Kalku refuse the whole request, every run.
MAX_NAME = 255
MAX_DOC_NUMBER = 200
MAX_SUPPLIER_ID = 50


def _round(value):
    return round(value or 0.0, 4)


def _product_key(product):
    """The product's internal reference without surrounding spaces, or None."""
    return (product.default_code or "").strip() or None


def _cut(value, size):
    return value[:size] if value else value


def _product_name(product):
    """The variant's name with its attribute values ("Camisa (S)"), without the
    internal reference that Odoo prefixes by default."""
    return _cut(product.with_context(display_default_code=False).display_name, MAX_NAME)


def _partner_identity(partner):
    """(key, vat) of a partner's commercial entity; key falls back to ODOO-<id>."""
    commercial = partner.commercial_partner_id
    vat = (commercial.vat or "").strip() or None
    return vat or "ODOO-%s" % commercial.id, vat, commercial


class KalkuReaders(models.AbstractModel):
    _name = "kalku.readers"
    _description = "Kalku canonical row readers"

    @api.model
    def resolve_source(self, setting):
        if setting in SALES_SOURCES:
            return setting
        if setting == "auto":
            return "orders" if "sale.order" in self.env else "invoices"
        raise UserError(_("Unknown sales source: %s", setting))

    # Sales -------------------------------------------------------------

    @api.model
    def read_sales(self, source, since):
        """Sales documents written since ``since`` (all of them when None).

        Confirmed documents give one row per product line; documents that are
        not confirmed, or have no row left after the skips, go to ``retract``.
        ``documents`` lists every document number read, in either list.
        """
        skipped = {"no_default_code": 0, "service": 0, "no_warehouse_code": 0}
        if source == "orders":
            documents = self._read_orders(since)
        elif source == "invoices":
            documents = self._read_invoices(since)
        else:
            raise UserError(_("Unknown sales source: %s", source))

        rows, retract, names = [], [], []
        for name, confirmed, header, lines in documents:
            if not name or name == "/":
                # A draft that never got a number was never sent to Kalku.
                continue
            # Cut once, so rows, retractions and the document list agree.
            name = _cut(name, MAX_DOC_NUMBER)
            names.append(name)
            doc_rows = []
            if confirmed:
                for product, values, warehouse in lines:
                    key = self._skip_reason(product, skipped)
                    if key is None:
                        continue
                    location = (warehouse.code or "").strip() or None
                    if location is None:
                        skipped["no_warehouse_code"] += 1
                    doc_rows.append(
                        dict(header, doc_number=name, product_key=key,
                             product_name=_product_name(product), location=location, **values)
                    )
            if doc_rows:
                rows.extend(doc_rows)
            else:
                retract.append(name)
        return {
            "source": source,
            "rows": rows,
            "retract": retract,
            "skipped": skipped,
            "documents": names,
        }

    @api.model
    def count_sales(self, source, since):
        """How many documents ``read_sales(source, since)`` would read."""
        if source == "orders":
            if "sale.order" not in self.env:
                return 0
            return self.env["sale.order"].search_count(self._window(since))
        if source == "invoices":
            return self.env["account.move"].search_count(self._invoice_domain(since))
        raise UserError(_("Unknown sales source: %s", source))

    def _window(self, since):
        domain = [("company_id", "=", self.env.company.id)]
        if since:
            domain.append(("write_date", ">=", since))
        return domain

    def _header(self, name, doc_date, partner):
        customer_key, _vat, commercial = _partner_identity(partner)
        return {
            "date": doc_date.isoformat(),
            "customer_key": customer_key,
            "customer_name": _cut(commercial.name, MAX_NAME),
            "cost": None,
            "delivery_point": None,
            "doc_type": None,
            "doc_number": name,
        }

    def _read_orders(self, since):
        if "sale.order" not in self.env:
            raise UserError(
                _("The Sales app is not installed: send sales from invoices instead.")
            )
        orders = self.env["sale.order"].search(self._window(since), order="id")
        has_warehouse = "warehouse_id" in orders._fields
        Warehouse = self.env["stock.warehouse"]
        for order in orders:
            warehouse = order.warehouse_id if has_warehouse else Warehouse
            # date_order is stored in UTC; its UTC date is the document date.
            header = self._header(order.name, order.date_order.date(), order.partner_id)
            lines = [
                (
                    line.product_id,
                    {
                        "quantity": _round(line.product_uom_qty),
                        "amount": _round(line.price_subtotal),
                        "unit_price": line.price_unit,
                    },
                    warehouse,
                )
                for line in order.order_line
                if not line.display_type and line.product_id
            ]
            yield order.name, order.state in CONFIRMED_ORDER_STATES, header, lines

    def _invoice_domain(self, since):
        return self._window(since) + [("move_type", "in", ("out_invoice", "out_refund"))]

    def _read_invoices(self, since):
        moves = self.env["account.move"].search(self._invoice_domain(since), order="id")
        Warehouse = self.env["stock.warehouse"]
        for move in moves:
            sign = -1 if move.move_type == "out_refund" else 1
            header = self._header(
                move.name, move.invoice_date or move.date, move.partner_id
            )
            lines = []
            for line in move.invoice_line_ids:
                if line.display_type != "product" or not line.product_id:
                    continue
                warehouse = Warehouse
                if "sale_line_ids" in line._fields and line.sale_line_ids:
                    order = line.sale_line_ids[:1].order_id
                    if "warehouse_id" in order._fields:
                        warehouse = order.warehouse_id
                lines.append(
                    (
                        line.product_id,
                        {
                            "quantity": _round(sign * line.quantity),
                            "amount": _round(sign * line.price_subtotal),
                            "unit_price": line.price_unit,
                        },
                        warehouse,
                    )
                )
            yield move.name, move.state == "posted", header, lines

    @staticmethod
    def _skip_reason(product, skipped):
        """The product's key, or None after counting why it is skipped."""
        if is_service(product):
            skipped["service"] += 1
            return None
        key = _product_key(product)
        if key is None:
            skipped["no_default_code"] += 1
        return key

    # Stock -------------------------------------------------------------

    @api.model
    def read_stock(self, as_of):
        """One row per storable product of the company: on-hand quantity on
        internal locations, every warehouse summed, zero included."""
        company = self.env.company
        skipped = {"no_default_code": 0, "service": 0}
        products = self.env["product.product"].search(
            [("company_id", "in", (False, company.id))], order="id"
        )
        keyed = []
        for product in products:
            # Services are counted; other goods without stock tracking are not
            # inventory and are left out silently.
            if not is_service(product) and not is_storable(product):
                continue
            key = self._skip_reason(product, skipped)
            if key is not None:
                keyed.append((product, key))

        on_hand = {}
        if keyed:
            groups = self.env["stock.quant"]._read_group(
                [
                    ("product_id", "in", [product.id for product, _key in keyed]),
                    ("location_id.usage", "=", "internal"),
                    ("company_id", "=", company.id),
                ],
                groupby=["product_id"],
                aggregates=["quantity:sum"],
            )
            on_hand = {product.id: quantity for product, quantity in groups}

        rows = [
            {
                "date": as_of.isoformat(),
                "product_key": key,
                "quantity": _round(on_hand.get(product.id)),
            }
            for product, key in keyed
        ]
        return {"rows": rows, "skipped": skipped}

    # Supplier terms ----------------------------------------------------

    @api.model
    def read_supplier_terms(self):
        """One row per (product, supplier). A line set on a template applies to
        each of its variants. Lines are read by lowest sequence, then id: the
        first line of a product is its preferred supplier, and the first line of
        a (product, supplier) pair stands for that supplier (price tiers, other
        contacts of the same company and template lines under a variant line
        are not sent again). Lines outside their validity dates and lines of
        archived suppliers are ignored."""
        company = self.env.company
        today = fields.Date.context_today(self)
        skipped = {"no_default_code": 0, "service": 0}
        infos = self.env["product.supplierinfo"].search(
            [("company_id", "in", (False, company.id))], order="sequence, id"
        )
        rows, preferred_seen, pairs_seen = [], set(), set()
        for info in infos:
            if (info.date_start and info.date_start > today) or (
                info.date_end and info.date_end < today
            ):
                continue
            if not info.partner_id.active or not info.partner_id.commercial_partner_id.active:
                continue
            products = info.product_id or info.product_tmpl_id.product_variant_ids
            supplier_key, vat, supplier = _partner_identity(info.partner_id)
            for product in products:
                if not product.active or product.company_id not in (company, company.browse()):
                    continue
                if (product.id, supplier_key) in pairs_seen:
                    continue
                pairs_seen.add((product.id, supplier_key))
                key = self._skip_reason(product, skipped)
                if key is None:
                    continue
                rows.append(
                    {
                        "product_key": key,
                        "supplier_key": supplier_key,
                        "supplier_name": _cut(supplier.name, MAX_NAME),
                        "supplier_id": _cut(vat, MAX_SUPPLIER_ID),
                        "lead_time_days": max(0, info.delay or 0),
                        "min_order_qty": _round(info.min_qty) or None,
                        "unit_price": _round(info.price) or None,
                        "is_preferred": product.id not in preferred_seen,
                    }
                )
                preferred_seen.add(product.id)
        return {"rows": rows, "skipped": skipped}
