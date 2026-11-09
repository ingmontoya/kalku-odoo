{
    "name": "Kalku Connector",
    "version": "18.0.1.0.0",
    "summary": "Send sales, stock and supplier lead times from Odoo to Kalku",
    "description": """
Reads sales, stock and supplier lead times from Odoo and sends them to Kalku,
which computes the demand forecast and the reorder points. This module does not
forecast anything and does not write to Odoo core models. It does not work on
Odoo Online (SaaS).
""",
    "author": "Mauricio Montoya",
    "website": "https://kalku.co",
    "license": "LGPL-3",
    "category": "Inventory/Inventory",
    "depends": ["stock", "purchase"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/ir.model.access.csv",
        "views/kalku_sync_log_views.xml",
        "views/res_config_settings_views.xml",
        "data/ir_cron.xml",
    ],
    "images": ["static/description/icon.png"],
    "installable": True,
    "application": False,
}
