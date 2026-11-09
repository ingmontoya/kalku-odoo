# Kalku Connector

Módulo de Odoo 18 (LGPL-3) que lee las ventas, el stock y los tiempos de entrega
de los proveedores en Odoo y los envía a [Kalku](https://kalku.co), un servicio
de pronóstico de demanda y planeación de inventario.

## Qué hace

- Envía a tu cuenta de Kalku las ventas (pedidos de venta o facturas de
  cliente), el stock por almacén y los tiempos de entrega de los proveedores.
- El pronóstico y los puntos de reorden se calculan en Kalku. Este módulo solo
  lee tu Odoo y envía los datos; no pronostica nada.
- Nunca escribe en los modelos del núcleo de Odoo. Guarda su configuración como
  claves de `ir.config_parameter` bajo `kalku.*` y lleva su propio registro de
  sincronización (`kalku.sync.log`, Inventario > Configuración > Kalku >
  Registro de sincronización).
- Traducciones: `i18n/kalku_connector.pot` se exporta desde Odoo; `es.po` se
  deriva de él.

## Datos que salen de Odoo

Usar este módulo requiere una cuenta de Kalku (un servicio externo). Envía:

- Líneas de venta: fecha, número de documento, referencia interna y nombre del
  producto, cantidad, importe sin impuestos, precio unitario y código del
  almacén.
- Nombres de clientes y proveedores con su número de identificación tributaria (VAT),
  usada como identificador.
- Stock por producto.
- Tiempo de entrega, cantidad mínima y precio de cada proveedor.

Los importes y los precios se envían en la moneda del documento, sin
conversión.

Las únicas escrituras del módulo en Odoo son su configuración y su propio
registro de sincronización.

## Límites

- **Odoo Online (SaaS) no es compatible**: requiere un módulo a medida, y Odoo
  Online no lo permite. Odoo.sh y las instalaciones propias (on-premise) sí
  funcionan.
- Los productos sin referencia interna (`default_code`) se omiten y se reportan
  en el registro de sincronización. Los servicios también se omiten.
- La URL de Kalku debe usar https (http solo se acepta para localhost y
  host.docker.internal durante el desarrollo).

## Configuración

1. En Kalku ve a Configuración > Integraciones > "Odoo (módulo)" y pulsa
   "Generar token".
2. En Odoo ve a Ajustes > Kalku, pega la URL de tu cuenta y el token, y elige
   qué enviar.

El token se guarda en `ir.config_parameter` (`kalku.token`) y no se vuelve a
mostrar después de guardarlo.

## Desarrollo

Las pruebas de ventas necesitan `sale_stock` (el almacén del pedido), que el
módulo no declara como dependencia; por eso se instala junto con él:

```bash
docker compose run --rm odoo odoo -d kalku_test -i kalku_connector,sale_stock \
  --test-enable --test-tags /kalku_connector --stop-after-init --without-demo=False
docker compose down -v
```

Sin `sale_stock` las pruebas de ventas se marcan como omitidas.

## Licencia

LGPL-3. Consulta `LICENSE`.
