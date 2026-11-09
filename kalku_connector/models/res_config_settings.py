from urllib.parse import urlparse

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .kalku_client import KalkuError

PARAM_URL = "kalku.url"
PARAM_TOKEN = "kalku.token"
PARAM_SYNC_SALES = "kalku.sync_sales"
PARAM_SYNC_STOCK = "kalku.sync_stock"
PARAM_SYNC_SUPPLIER_TERMS = "kalku.sync_supplier_terms"
PARAM_SALES_SOURCE = "kalku.sales_source"

# Plain http is accepted only for local development.
ALLOWED_HTTP_HOSTS = {"localhost", "127.0.0.1", "host.docker.internal"}


def is_allowed_kalku_url(url):
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError:
        return False
    scheme = (parsed.scheme or "").lower()
    if scheme == "https":
        return bool(hostname)
    return scheme == "http" and hostname in ALLOWED_HTTP_HOSTS


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    kalku_url = fields.Char(string="Kalku URL")
    kalku_token = fields.Char(string="Kalku token", groups="base.group_system")
    kalku_sync_sales = fields.Boolean(string="Send sales")
    kalku_sync_stock = fields.Boolean(string="Send stock")
    kalku_sync_supplier_terms = fields.Boolean(string="Send supplier lead times")
    kalku_sales_source = fields.Selection(
        [
            ("auto", "Automatic"),
            ("orders", "Sales orders"),
            ("invoices", "Customer invoices"),
        ],
        string="Sales source",
        default="auto",
        required=True,
    )

    @api.constrains("kalku_url")
    def _check_kalku_url(self):
        for settings in self:
            url = (settings.kalku_url or "").strip()
            if not url:
                continue
            if not is_allowed_kalku_url(url):
                raise ValidationError(_("Kalku URL must use https."))

    @api.model
    def get_values(self):
        res = super().get_values()
        params = self.env["ir.config_parameter"].sudo()
        res.update(
            kalku_url=params.get_param(PARAM_URL, ""),
            kalku_sync_sales=params.get_param(PARAM_SYNC_SALES) == "True",
            kalku_sync_stock=params.get_param(PARAM_SYNC_STOCK) == "True",
            kalku_sync_supplier_terms=params.get_param(PARAM_SYNC_SUPPLIER_TERMS)
            == "True",
            kalku_sales_source=params.get_param(PARAM_SALES_SOURCE, "auto"),
            # The token is never read back into the view.
            kalku_token="",
        )
        return res

    def set_values(self):
        super().set_values()
        params = self.env["ir.config_parameter"].sudo()
        params.set_param(PARAM_URL, (self.kalku_url or "").strip())
        params.set_param(PARAM_SYNC_SALES, str(bool(self.kalku_sync_sales)))
        params.set_param(PARAM_SYNC_STOCK, str(bool(self.kalku_sync_stock)))
        params.set_param(
            PARAM_SYNC_SUPPLIER_TERMS, str(bool(self.kalku_sync_supplier_terms))
        )
        params.set_param(PARAM_SALES_SOURCE, self.kalku_sales_source or "auto")
        # An empty token field means "keep the one already stored".
        if self.sudo().kalku_token:
            params.set_param(PARAM_TOKEN, self.sudo().kalku_token.strip())

    def action_kalku_test_connection(self):
        """Ping Kalku with the URL and token on screen (saved or not)."""
        self.ensure_one()
        try:
            answer = self.env["kalku.sync"].test_connection(
                url=self.kalku_url, token=self.sudo().kalku_token
            )
        except KalkuError as exc:
            raise UserError(exc.message) from exc
        return _notification(
            _("Connected to Kalku: %s", answer.get("name") or answer.get("tenant")), "success"
        )

    def action_kalku_sync_now(self):
        """Sync with the saved settings: in the request when small, else via the cron."""
        self.ensure_one()
        sync = self.env["kalku.sync"]
        if not sync.runs_in_request():
            self.env.ref("kalku_connector.ir_cron_kalku_sync").sudo()._trigger()
            return _notification(
                _("Sync started; check Inventory > Configuration > Kalku > Sync log."), "info"
            )
        log = sync.run()
        if log.state == "failed":
            return _notification(_("Sync failed: %s", log.error), "danger")
        return _notification(
            _("Sync finished; check Inventory > Configuration > Kalku > Sync log."), "success"
        )


def _notification(message, kind):
    return {
        "type": "ir.actions.client",
        "tag": "display_notification",
        "params": {
            "title": "Kalku",
            "message": message,
            "type": kind,
            "sticky": kind == "danger",
        },
    }
