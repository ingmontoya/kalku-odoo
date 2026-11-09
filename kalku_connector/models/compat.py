"""Field names that differ between Odoo versions (this is the 17.0 branch).

On 17.0, ``product.type`` says service (modules such as event or website_slides
add ``detailed_type`` values that still map to ``type == 'service'``) and
``detailed_type == 'product'`` says storable. On 18.0, ``type`` says service
and ``is_storable`` says storable.
"""

# 'done' (locked) no longer exists on 17/18; kept for parity with Kalku's pull path.
CONFIRMED_ORDER_STATES = ("sale", "done")


def is_service(product):
    return product.type == "service"


def is_storable(product):
    return product.detailed_type == "product"
