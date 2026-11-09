from odoo import fields, models


class KalkuSyncLog(models.Model):
    _name = "kalku.sync.log"
    _description = "Kalku sync log"
    _order = "started_at desc, id desc"

    started_at = fields.Datetime(default=fields.Datetime.now, readonly=True)
    finished_at = fields.Datetime(readonly=True)
    sync_type = fields.Selection(
        [("full", "Full"), ("incremental", "Incremental")],
        required=True,
        readonly=True,
    )
    dataset = fields.Selection(
        [
            ("sales", "Sales"),
            ("stock", "Stock"),
            ("supplier_terms", "Supplier terms"),
            ("all", "All"),
        ],
        required=True,
        readonly=True,
    )
    state = fields.Selection(
        [("running", "Running"), ("done", "Done"), ("failed", "Failed")],
        required=True,
        default="running",
        readonly=True,
    )
    documents_read = fields.Integer(readonly=True)
    rows_sent = fields.Integer(readonly=True)
    skipped_no_default_code = fields.Integer(
        string="Skipped: no internal reference", readonly=True
    )
    skipped_service = fields.Integer(string="Skipped: services", readonly=True)
    skipped_no_warehouse_code = fields.Integer(
        string="Skipped: no warehouse code", readonly=True
    )
    kalku_response = fields.Text(string="Kalku response (JSON)", readonly=True)
    error = fields.Text(readonly=True)
