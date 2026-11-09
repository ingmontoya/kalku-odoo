"""Field names that differ between Odoo versions (this is the 17.0 branch).

On 17.0, ``product.detailed_type`` carries both facts: ``'service'`` and
``'product'`` (storable). On 18.0, ``type`` says service and ``is_storable``
says storable.
"""

# 'done' (locked) no longer exists on 17/18; kept for parity with Kalku's pull path.
CONFIRMED_ORDER_STATES = ("sale", "done")


def is_service(product):
    return product.detailed_type == "service"


def is_storable(product):
    return product.detailed_type == "product"
