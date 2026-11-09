# Kalku Connector

Odoo 18 module (LGPL-3) that reads sales, stock and supplier lead times from
Odoo and sends them to [Kalku](https://kalku.co), a demand-forecasting and
inventory-planning service.

## What it does

- Sends sales (sales orders or customer invoices), stock per warehouse and
  supplier lead times to your Kalku account.
- The forecast and the reorder points are computed in Kalku. This module only
  reads your Odoo and sends the data; it does not forecast anything.
- It never writes to Odoo core models. It stores its settings as
  `ir.config_parameter` keys under `kalku.*` and keeps its own sync log
  (`kalku.sync.log`, Inventory > Configuration > Kalku > Sync log).

## Limits

- **Odoo Online (SaaS) is not supported**: it needs a custom module, which Odoo
  Online does not allow. Odoo.sh and on-premise installs work.
- Products without an internal reference (`default_code`) are skipped and
  reported in the sync log. Services are skipped too.
- The Kalku URL must use https (plain http is accepted only for localhost and
  host.docker.internal during development).

## Setup

1. In Kalku go to Configuración > Integraciones > "Odoo (módulo)" and press
   "Generar token".
2. In Odoo go to Settings > Kalku, paste the URL of your account and the token,
   and choose what to send.

The token is stored in `ir.config_parameter` (`kalku.token`) and is never
shown again after saving.

## Development

```bash
docker compose run --rm odoo odoo -d kalku_test -i kalku_connector \
  --test-enable --test-tags /kalku_connector --stop-after-init
docker compose down -v
```

## License

LGPL-3. See `LICENSE`.
