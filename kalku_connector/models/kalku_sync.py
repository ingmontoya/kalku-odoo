"""Send the readers' canonical rows to Kalku.

One run: ping, sales (previous-source retraction first when the source
changed), stock, supplier terms, then ``sync/complete``. The sales watermark
only moves after Kalku confirmed the run, so anything that fails is read again
by the next run; Kalku deduplicates by document number, so resending a chunk
loads nothing twice.
"""

import json
import logging
from datetime import timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .kalku_client import KalkuAuthError, KalkuClient, KalkuError
from .kalku_readers import SALES_SOURCES
from .res_config_settings import (
    PARAM_SALES_SOURCE,
    PARAM_SYNC_SALES,
    PARAM_SYNC_STOCK,
    PARAM_SYNC_SUPPLIER_TERMS,
    PARAM_TOKEN,
    PARAM_URL,
)

_logger = logging.getLogger(__name__)

PARAM_LAST_SYNC_SALES = "kalku.last_sync_at.sales"
# The source the watermark belongs to: another source starts from the beginning.
PARAM_LAST_SYNC_SALES_SOURCE = "kalku.last_sync_source.sales"

# Kalku's limits per request (ConnectorSalesRequest, ConnectorInventoryRequest,
# ConnectorSupplierTermsRequest).
CHUNK_DOCUMENTS = 500
CHUNK_PRODUCTS = 500
CHUNK_NUMBERS = 5000
# "Sync now" runs in the request up to this many sales documents.
MAX_DOCUMENTS_IN_REQUEST = 2000
# Documents edited shortly before the last run are read again.
OVERLAP = timedelta(hours=24)
SKIP_REASONS = ("no_default_code", "service", "no_warehouse_code")
DATASETS = ("sales", "stock", "supplier_terms")


def _chunks_by(rows, key, size):
    """Lists of rows with at most ``size`` distinct ``key`` values each.

    Rows sharing a key always land in the same list, so a document (or a
    product) never spans two requests.
    """
    groups = {}
    for row in rows:
        groups.setdefault(row[key], []).append(row)
    batch, keys = [], 0
    for group in groups.values():
        if keys == size:
            yield batch
            batch, keys = [], 0
        batch.extend(group)
        keys += 1
    if batch:
        yield batch


def _slices(items, size):
    return [items[start:start + size] for start in range(0, len(items), size)]


def _add_answer(total, answer):
    """Sum Kalku's counts across chunks; keep the last value of anything else."""
    for key, value in (answer or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value
        else:
            total[key] = value


class KalkuSync(models.AbstractModel):
    _name = "kalku.sync"
    _description = "Kalku sync"

    # Settings ------------------------------------------------------------

    @api.model
    def _settings(self):
        get = self.env["ir.config_parameter"].sudo().get_param
        return {
            "url": (get(PARAM_URL) or "").strip(),
            "token": (get(PARAM_TOKEN) or "").strip(),
            "sales": get(PARAM_SYNC_SALES) == "True",
            "stock": get(PARAM_SYNC_STOCK) == "True",
            "supplier_terms": get(PARAM_SYNC_SUPPLIER_TERMS) == "True",
            "source": get(PARAM_SALES_SOURCE) or "auto",
        }

    @staticmethod
    def _ticked(settings):
        return [name for name in DATASETS if settings[name]]

    @api.model
    def _check_configured(self, settings):
        if not settings["url"] or not settings["token"]:
            raise UserError(_("Set the Kalku URL and token first."))
        if not self._ticked(settings):
            raise UserError(_("Choose at least one dataset to send."))

    @api.model
    def _sales_source(self, settings):
        return self.env["kalku.readers"].resolve_source(settings["source"] or "auto")

    @api.model
    def _sales_since(self, source):
        """Start of the sales window: the watermark minus the overlap, or None
        when no run of this source was ever confirmed."""
        get = self.env["ir.config_parameter"].sudo().get_param
        watermark = get(PARAM_LAST_SYNC_SALES)
        if not watermark or get(PARAM_LAST_SYNC_SALES_SOURCE) != source:
            return None
        return fields.Datetime.from_string(watermark) - OVERLAP

    # Entry points -----------------------------------------------------------

    @api.model
    def test_connection(self, url=None, token=None):
        """Kalku's ping answer; ``url`` / ``token`` override the stored ones."""
        settings = self._settings()
        url = (url or "").strip() or settings["url"]
        token = (token or "").strip() or settings["token"]
        if not url or not token:
            raise UserError(_("Set the Kalku URL and token first."))
        return KalkuClient(url, token).ping()

    @api.model
    def runs_in_request(self):
        """Whether "Sync now" can run without handing the work to the cron."""
        settings = self._settings()
        if not settings["sales"]:
            return True
        source = self._sales_source(settings)
        count = self.env["kalku.readers"].count_sales(source, self._sales_since(source))
        return count <= MAX_DOCUMENTS_IN_REQUEST

    @api.model
    def run_scheduled(self):
        """Cron entry point: never raises, does nothing until configured."""
        settings = self._settings()
        if not settings["url"] or not settings["token"] or not self._ticked(settings):
            _logger.info("Kalku sync skipped: the module is not configured.")
            return
        try:
            self.run()
        except Exception:
            _logger.exception("Kalku sync could not run.")

    @api.model
    def run(self, sync_type="incremental"):
        settings = self._settings()
        self._check_configured(settings)
        ticked = self._ticked(settings)
        source = self._sales_source(settings) if settings["sales"] else None
        if source and self._sales_since(source) is None:
            sync_type = "full"
        started = fields.Datetime.now()
        log = self.env["kalku.sync.log"].sudo().create(
            {
                "started_at": started,
                "sync_type": sync_type,
                "dataset": ticked[0] if len(ticked) == 1 else "all",
            }
        )
        progress = {
            "sync_type": sync_type,
            "datasets": {},
            "skipped": dict.fromkeys(SKIP_REASONS, 0),
            "documents_read": 0,
            "rows_sent": 0,
        }
        client = KalkuClient(settings["url"], settings["token"])
        try:
            with self.env.cr.savepoint():
                self._push(client, settings, source, started, progress)
                client.complete(self._complete_payload(started, progress, "completed"))
        except KalkuAuthError as exc:
            # The token cannot write anything, not even the failure.
            _logger.info("Kalku refused the token: %s", exc.message)
            return self._finish(log, progress, "failed", exc.message)
        except Exception as exc:
            if isinstance(exc, KalkuError):
                message, errors = exc.message, exc.errors
                _logger.info("Kalku sync failed: %s", message)
            elif isinstance(exc, UserError):
                # A setting Odoo cannot honour (e.g. orders without the Sales app).
                message, errors = exc.args[0], None
                _logger.info("Kalku sync failed: %s", message)
            else:
                message, errors = str(exc), None
                _logger.exception("Kalku sync failed.")
            try:
                client.complete(self._complete_payload(started, progress, "failed", message))
            except Exception as complete_exc:
                _logger.info("Kalku did not record the failed run: %s", complete_exc)
            return self._finish(log, progress, "failed", message, errors)

        if source:
            params = self.env["ir.config_parameter"].sudo()
            params.set_param(PARAM_LAST_SYNC_SALES, fields.Datetime.to_string(started))
            params.set_param(PARAM_LAST_SYNC_SALES_SOURCE, source)
        return self._finish(log, progress, "done")

    # Steps ------------------------------------------------------------------

    def _push(self, client, settings, source, started, progress):
        ping = client.ping()
        readers = self.env["kalku.readers"]
        if source:
            self._push_sales(client, source, ping, progress)
        if settings["stock"]:
            self._push_snapshot(
                client.push_inventory, "inventory", readers.read_stock(started.date()), progress
            )
        if settings["supplier_terms"]:
            self._push_snapshot(
                client.push_supplier_terms,
                "supplier_terms",
                readers.read_supplier_terms(),
                progress,
            )

    def _push_sales(self, client, source, ping, progress):
        readers = self.env["kalku.readers"]
        total = progress["datasets"].setdefault("sales", {})
        since = None if progress["sync_type"] == "full" else self._sales_since(source)

        loaded = ping.get("sales_source_loaded")
        if loaded in SALES_SOURCES and loaded != source:
            # Orders and invoices are the same sales: retract every document of
            # the old source, then load the new one from the start.
            progress["sync_type"], since = "full", None
            numbers = readers.read_sales(loaded, None)["documents"]
            for part in _slices(numbers, CHUNK_NUMBERS):
                _add_answer(
                    total,
                    client.push_sales(
                        {
                            "source": source,
                            "previous_source": loaded,
                            "rows": [],
                            "retract": [],
                            "previous_source_retract": part,
                        }
                    ),
                )

        sales = readers.read_sales(source, since)
        progress["documents_read"] += len(sales["documents"])
        self._add_skipped(progress, sales["skipped"])
        row_chunks = list(_chunks_by(sales["rows"], "doc_number", CHUNK_DOCUMENTS))
        retract_parts = _slices(sales["retract"], CHUNK_NUMBERS)
        for index in range(max(len(row_chunks), len(retract_parts))):
            rows = row_chunks[index] if index < len(row_chunks) else []
            retract = retract_parts[index] if index < len(retract_parts) else []
            _add_answer(
                total,
                client.push_sales(
                    {
                        "source": source,
                        "rows": rows,
                        "retract": retract,
                        "previous_source_retract": [],
                    }
                ),
            )
            progress["rows_sent"] += len(rows)

    def _push_snapshot(self, send, name, result, progress):
        total = progress["datasets"].setdefault(name, {})
        self._add_skipped(progress, result["skipped"])
        for rows in _chunks_by(result["rows"], "product_key", CHUNK_PRODUCTS):
            _add_answer(total, send(rows))
            progress["rows_sent"] += len(rows)

    @staticmethod
    def _add_skipped(progress, skipped):
        for reason, count in skipped.items():
            progress["skipped"][reason] = progress["skipped"].get(reason, 0) + count

    @staticmethod
    def _datasets_payload(progress):
        return dict(progress["datasets"], skipped=dict(progress["skipped"]))

    def _complete_payload(self, started, progress, status, error=None):
        payload = {
            "sync_type": progress["sync_type"],
            "status": status,
            "started_at": started.replace(tzinfo=timezone.utc).isoformat(),
            "datasets": self._datasets_payload(progress),
        }
        if error:
            payload["error"] = error[:2000]
        return payload

    def _finish(self, log, progress, state, error=None, errors=None):
        response = self._datasets_payload(progress)
        if errors:
            response["errors"] = errors
        log.write(
            {
                "state": state,
                "error": error,
                "finished_at": fields.Datetime.now(),
                "sync_type": progress["sync_type"],
                "documents_read": progress["documents_read"],
                "rows_sent": progress["rows_sent"],
                "skipped_no_default_code": progress["skipped"]["no_default_code"],
                "skipped_service": progress["skipped"]["service"],
                "skipped_no_warehouse_code": progress["skipped"]["no_warehouse_code"],
                "kalku_response": json.dumps(response, ensure_ascii=False, indent=2),
            }
        )
        return log
