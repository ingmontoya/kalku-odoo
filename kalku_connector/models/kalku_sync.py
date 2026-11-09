"""Send the readers' canonical rows to Kalku.

One run: ping, sales (previous-source retraction first when the source
changed), stock, supplier terms, then ``sync/complete``. Every run is pinned to
the company chosen in the settings and runs as superuser there. The sales
watermark only moves after Kalku confirmed the run, so anything that fails is
read again by the next run; Kalku deduplicates by document number, so resending
a chunk loads nothing twice.
"""

import json
import logging
from datetime import timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .kalku_client import KalkuAuthError, KalkuClient, KalkuError
from .kalku_readers import SALES_SOURCES
from .res_config_settings import (
    PARAM_COMPANY,
    PARAM_SALES_SOURCE,
    PARAM_SYNC_SALES,
    PARAM_SYNC_STOCK,
    PARAM_SYNC_SUPPLIER_TERMS,
    PARAM_TOKEN,
    PARAM_URL,
    is_allowed_kalku_url,
)

_logger = logging.getLogger(__name__)

PARAM_LAST_SYNC_SALES = "kalku.last_sync_at.sales"
# The source the watermark belongs to: another source starts from the beginning.
PARAM_LAST_SYNC_SALES_SOURCE = "kalku.last_sync_source.sales"
# Set (committed at once) before a switch retracts the old source's documents,
# cleared when a run confirms: an interrupted switch is resumed by the next run
# even when Kalku already reports the new source and none was confirmed here.
PARAM_PENDING_PREVIOUS_SOURCE = "kalku.pending_previous_source.sales"

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
# Postgres advisory lock that keeps two runs (cron and button) from overlapping.
LOCK_KEY = 0x4B414C4B55


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
    """Every method is private: the settings buttons and the cron are the only
    ways in, so no user can call a sync (or send the token elsewhere) through
    RPC."""

    _name = "kalku.sync"
    _description = "Kalku sync"

    # Settings ------------------------------------------------------------

    @api.model
    def _settings(self):
        get = self.env["ir.config_parameter"].sudo().get_param
        try:
            company = int(get(PARAM_COMPANY) or 0)
        except ValueError:
            company = 0
        return {
            "url": (get(PARAM_URL) or "").strip(),
            "token": (get(PARAM_TOKEN) or "").strip(),
            "company": company,
            "sales": get(PARAM_SYNC_SALES) == "True",
            "stock": get(PARAM_SYNC_STOCK) == "True",
            "supplier_terms": get(PARAM_SYNC_SUPPLIER_TERMS) == "True",
            "source": get(PARAM_SALES_SOURCE) or "auto",
        }

    @staticmethod
    def _ticked(settings):
        return [name for name in DATASETS if settings[name]]

    @api.model
    def _company(self, settings):
        return self.env["res.company"].sudo().browse(settings["company"]).exists()

    @api.model
    def _check_configured(self, settings):
        if not settings["url"] or not settings["token"]:
            raise UserError(_("Set the Kalku URL and token first."))
        if not is_allowed_kalku_url(settings["url"]):
            raise UserError(_("Kalku URL must use https."))
        if not self._company(settings):
            raise UserError(_("Choose the company whose data is sent to Kalku."))
        if not self._ticked(settings):
            raise UserError(_("Choose at least one dataset to send."))

    @api.model
    def _pinned(self, settings):
        """Superuser in the company chosen in the settings, never the active one
        of whoever pressed the button: readers see that company's records only."""
        return self.sudo().with_company(self._company(settings))

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

    # Entry points (called by the settings buttons and the cron) -------------

    @api.model
    def _test_connection(self, url=None, token=None):
        """Kalku's ping answer for the URL and token on screen.

        The saved token is only ever sent to the saved URL: testing another URL
        needs the token typed again.
        """
        settings = self._settings()
        typed_url = (url or "").strip()
        typed_token = (token or "").strip()
        if typed_url and typed_url.rstrip("/") != settings["url"].rstrip("/"):
            if not typed_token:
                raise UserError(_("Type the token to test a URL other than the saved one."))
            target, secret = typed_url, typed_token
        else:
            target, secret = settings["url"], typed_token or settings["token"]
        if not target or not secret:
            raise UserError(_("Set the Kalku URL and token first."))
        if not is_allowed_kalku_url(target):
            raise UserError(_("Kalku URL must use https."))
        return KalkuClient(target, secret).ping()

    @api.model
    def _runs_in_request(self):
        """Whether "Sync now" can run without handing the work to the cron."""
        settings = self._settings()
        self._check_configured(settings)
        if not settings["sales"]:
            return True
        sync = self._pinned(settings)
        source = sync._sales_source(settings)
        count = sync.env["kalku.readers"].count_sales(source, sync._sales_since(source))
        return count <= MAX_DOCUMENTS_IN_REQUEST

    @api.model
    def _run_scheduled(self):
        """Cron entry point: never raises, does nothing until configured."""
        try:
            self._check_configured(self._settings())
        except UserError as exc:
            _logger.info("Kalku sync skipped: %s", exc.args[0])
            return
        try:
            self._run()
        except UserError as exc:
            # Another run holds the lock.
            _logger.info("Kalku sync skipped: %s", exc.args[0])
        except Exception:
            _logger.exception("Kalku sync could not run.")

    @api.model
    def _run(self, sync_type="incremental"):
        settings = self._settings()
        self._check_configured(settings)
        return self._pinned(settings)._run_pinned(settings, sync_type)

    # Log on its own cursor ------------------------------------------------

    @api.model
    def _log_write(self, log_id, values):
        """Create or update a log row on a cursor of its own, committed at once,
        so the row outlives a run whose transaction is rolled back or whose
        worker is killed (Odoo's time limit). Returns the row's id."""
        with self.env.registry.cursor() as cr:
            Log = api.Environment(cr, self.env.uid, {})["kalku.sync.log"].sudo()
            if log_id:
                Log.browse(log_id).write(values)
            else:
                log_id = Log.create(values).id
        self.env["kalku.sync.log"].invalidate_model()
        return log_id

    @api.model
    def _log_values(self, log_id, field_names):
        """A log row read on a fresh cursor: the caller's transaction may have
        started before the row was committed and would not see it."""
        with self.env.registry.cursor() as cr:
            Log = api.Environment(cr, self.env.uid, {})["kalku.sync.log"].sudo()
            return Log.browse(log_id).read(field_names)[0]

    @api.model
    def _commit_param(self, key, value):
        """Set (or, with a falsy value, remove) a parameter on its own committed
        cursor, so it outlives this run's transaction. The run never writes this
        key on its own cursor, which keeps the two from contending for the row."""
        with self.env.registry.cursor() as cr:
            api.Environment(cr, self.env.uid, {})["ir.config_parameter"].sudo().set_param(
                key, value
            )

    @api.model
    def _close_stale_logs(self):
        """Runs still marked running died without finishing (the lock is free)."""
        message = _(
            "The sync stopped before finishing, for example because Odoo's time limit ended it."
        )
        with self.env.registry.cursor() as cr:
            Log = api.Environment(cr, self.env.uid, {})["kalku.sync.log"].sudo()
            Log.search([("state", "=", "running")]).write(
                {"state": "failed", "error": message, "finished_at": fields.Datetime.now()}
            )
        self.env["kalku.sync.log"].invalidate_model()

    @api.model
    def _lock(self):
        self.env.cr.execute("SELECT pg_try_advisory_xact_lock(%s)", [LOCK_KEY])
        if not self.env.cr.fetchone()[0]:
            raise UserError(_("A Kalku sync is already running."))

    # Run ------------------------------------------------------------------

    def _run_pinned(self, settings, sync_type):
        self._lock()
        self._close_stale_logs()
        ticked = self._ticked(settings)
        source = self._sales_source(settings) if settings["sales"] else None
        if source and self._sales_since(source) is None:
            sync_type = "full"
        started = fields.Datetime.now()
        log_id = self._log_write(
            None,
            {
                "started_at": started,
                "sync_type": sync_type,
                "dataset": ticked[0] if len(ticked) == 1 else "all",
            },
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
            ping = client.ping()
            previous = self._previous_source(source, ping) if source else None
            if previous:
                # Outside the savepoint and committed: survives whatever follows.
                self._commit_param(PARAM_PENDING_PREVIOUS_SOURCE, previous)
            with self.env.cr.savepoint():
                self._push(client, source, previous, started, progress, settings)
                client.complete(self._complete_payload(started, progress, "completed"))
        except KalkuAuthError as exc:
            # The token cannot write anything, not even the failure.
            _logger.info("Kalku refused the token: %s", exc.message)
            return self._finish(log_id, progress, "failed", exc.message)
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
            return self._finish(log_id, progress, "failed", message, errors)

        if source:
            params = self.env["ir.config_parameter"].sudo()
            params.set_param(PARAM_LAST_SYNC_SALES, fields.Datetime.to_string(started))
            params.set_param(PARAM_LAST_SYNC_SALES_SOURCE, source)
            # Always on the side cursor: this transaction may predate the marker
            # this very run committed, and would not see it to clear it.
            self._commit_param(PARAM_PENDING_PREVIOUS_SOURCE, False)
        return self._finish(log_id, progress, "done")

    # Steps ------------------------------------------------------------------

    def _push(self, client, source, previous, started, progress, settings):
        readers = self.env["kalku.readers"]
        if source:
            self._push_sales(client, source, previous, progress)
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

    def _previous_source(self, source, ping):
        """The source whose documents must leave Kalku before ``source`` loads.

        Kalku's ``sales_source_loaded`` says which source it holds, but it takes
        the new source on the first request of a switch. An interrupted switch
        is still named by the pending marker, or by the last source a run
        confirmed here.
        """
        get = self.env["ir.config_parameter"].sudo().get_param
        candidates = (
            ping.get("sales_source_loaded"),
            get(PARAM_PENDING_PREVIOUS_SOURCE),
            get(PARAM_LAST_SYNC_SALES_SOURCE),
        )
        for candidate in candidates:
            if candidate in SALES_SOURCES and candidate != source:
                return candidate
        return None

    def _push_sales(self, client, source, previous, progress):
        readers = self.env["kalku.readers"]
        total = progress["datasets"].setdefault("sales", {})
        since = None if progress["sync_type"] == "full" else self._sales_since(source)

        if previous:
            # Orders and invoices are the same sales: retract every document of
            # the old source (the whole list again after an interruption; Kalku
            # ignores numbers it no longer has), then load the new one from the
            # start.
            progress["sync_type"], since = "full", None
            numbers = readers.read_sales(previous, None)["documents"]
            for part in _slices(numbers, CHUNK_NUMBERS):
                _add_answer(
                    total,
                    client.push_sales(
                        {
                            "source": source,
                            "previous_source": previous,
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

    def _finish(self, log_id, progress, state, error=None, errors=None):
        response = self._datasets_payload(progress)
        if errors:
            response["errors"] = errors
        self._log_write(
            log_id,
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
            },
        )
        return self.env["kalku.sync.log"].browse(log_id)
