# Kalku Connector

Módulo de Odoo 17 (LGPL-3) que lee las ventas, el stock y los tiempos de entrega
de los proveedores en Odoo y los envía a [Kalku](https://kalku.co), un servicio
de pronóstico de demanda y planeación de inventario.

## Qué hace

- Envía a tu cuenta de Kalku las ventas (pedidos de venta o facturas de
  cliente), el stock de cada producto (la suma de todos los almacenes) y los
  tiempos de entrega de los proveedores.
- El pronóstico y los puntos de reorden se calculan en Kalku. Este módulo solo
  lee tu Odoo y envía los datos; no pronostica nada.
- Solo escribe en Odoo su propia configuración (claves de `ir.config_parameter`
  bajo `kalku.*`), su registro de sincronización (`kalku.sync.log`, Inventario >
  Configuración > Kalku > Registro de sincronización) y el aviso que pone en
  marcha su acción programada cuando una sincronización se deja para después.
  No cambia ningún otro dato de Odoo.
- Traducciones: `i18n/kalku_connector.pot` se exporta desde Odoo; `es.po` se
  deriva de él.

## Datos que salen de Odoo

Usar este módulo requiere una cuenta de Kalku (un servicio externo). Envía:

- Líneas de venta: fecha, número de documento, referencia interna y nombre del
  producto, cantidad, importe sin impuestos, precio unitario y código del
  almacén.
- Nombres de clientes y proveedores con su número de identificación tributaria (VAT),
  usada como identificador. Un cliente o proveedor sin VAT se identifica como
  `ODOO-<id>`, con el id interno de Odoo.
- Stock por producto (la suma de todos los almacenes).
- Tiempo de entrega, cantidad mínima y precio de cada proveedor.
- Los números de los documentos que dejaron de estar confirmados (borradores y
  cancelados), para que Kalku retire esas ventas si ya las tenía.
- Al final de cada sincronización, un resumen (cuántos documentos y filas se
  enviaron y cuántos se omitieron) y, si falló, el texto del error.

Los importes y los precios se envían en la moneda del documento, sin
conversión.

## Límites

- **Odoo Online (SaaS) no es compatible**: requiere un módulo a medida, y Odoo
  Online no lo permite. Odoo.sh y las instalaciones propias (on-premise) sí
  funcionan.
- Los productos sin referencia interna (`default_code`) se omiten y se reportan
  en el registro de sincronización. Los servicios también se omiten.
- Las líneas de venta de un almacén sin código sí se envían, al centro por
  defecto de Kalku; el registro las cuenta en "Sin código de almacén (centro por
  defecto)".
- La URL de Kalku debe usar https (http solo se acepta para localhost y
  host.docker.internal durante el desarrollo).

## Configuración

1. En Kalku ve a Configuración > Integraciones > "Odoo (módulo)" y pulsa
   "Generar token".
2. En Odoo ve a Ajustes > Kalku, pega la URL de tu cuenta y el token, y elige
   qué enviar.

El token se guarda en `ir.config_parameter` (`kalku.token`) y no se vuelve a
mostrar después de guardarlo.

"Probar conexión" consulta Kalku con la URL y el token que se ven en pantalla,
aunque todavía no se hayan guardado, y muestra el nombre de la empresa en Kalku.
El token guardado solo se envía a la URL guardada: para probar otra URL hay que
escribir el token de nuevo. Solo los administradores pueden usar estos botones.

En una base de datos con varias empresas, el campo "Empresa" indica cuál se
envía a Kalku. Por omisión es la empresa activa al guardar los ajustes, y se
respeta aunque después se sincronice con otra empresa activa.

## Sincronización

- "Sincronizar ahora" usa la configuración guardada. Si hay hasta 2000
  documentos de venta por leer, sincroniza en el momento; si hay más, deja la
  tarea a la acción programada y avisa que el resultado aparecerá en el registro
  de sincronización.
- La acción programada "Kalku: sincronización" corre una vez al día (la primera
  vez a las 02:30 UTC del día siguiente a la instalación; la hora se cambia en
  Ajustes > Técnico > Acciones programadas). Envía, de la empresa elegida en el
  campo "Empresa" de los ajustes, solo los datos marcados en "Qué enviar".
- La primera sincronización envía todas las ventas. Las siguientes envían los
  documentos modificados desde la última sincronización exitosa, con 24 horas de
  margen. El stock y los tiempos de entrega se envían completos cada vez.
- "Reenviar todas las ventas" vuelve a enviar todos los documentos de venta,
  como la primera sincronización. Úsalo después de ponerle referencia interna a
  productos que no la tenían o código a un almacén: esos cambios no modifican los
  documentos, así que las sincronizaciones normales no los vuelven a leer. Sigue
  la misma regla que "Sincronizar ahora": hasta 2000 documentos, en el momento;
  si son más, la acción programada.
- Si la fuente de las ventas cambia (de facturas a pedidos o al revés), primero
  se retiran en Kalku los documentos de la fuente anterior y luego se envía la
  nueva desde el inicio.
- Los datos van en lotes de 500 documentos o 500 productos. Si Kalku rechaza el
  token, la sincronización se detiene en ese punto; ante cualquier error, la
  siguiente vuelve a enviar el mismo periodo, y Kalku no duplica lo que ya
  recibió.
- Nunca corren dos sincronizaciones a la vez. Si se pulsa "Sincronizar ahora"
  mientras otra está en curso, se muestra el aviso "Ya hay una sincronización con
  Kalku en curso." Si es la acción programada la que coincide con otra, se salta
  esa vez sin crear fila en el registro y solo deja una línea informativa en el
  log del servidor.
- Si a la acción programada le falta configuración (la URL, el token, la empresa
  o algún dato marcado para enviar), deja una fila fallida en el registro con el
  motivo y no contacta a Kalku.
- Cada sincronización deja su fila en el registro aunque Odoo la interrumpa. Si
  una queda "En curso" porque Odoo detuvo el proceso, la siguiente la marca como
  fallida.

### Bases de datos grandes

Odoo detiene una acción programada que supera su límite de tiempo
(`limit_time_real_cron`; si vale -1, se usa `limit_time_real`, que por omisión es
de 120 segundos). La primera sincronización envía todas las ventas y en una base
grande puede superar ese límite: el proceso se detiene, no se confirma nada en
Kalku y la siguiente vuelve a empezar desde el principio. Para esa primera vez:

- Sube el límite en el archivo de configuración de Odoo, por ejemplo
  `limit_time_real_cron = 3600` (en segundos; 0 lo desactiva), y reinicia Odoo.
- O ejecuta la sincronización desde `odoo shell`, que no tiene ese límite:

  ```python
  env["kalku.sync"]._run()
  env.cr.commit()
  ```

Las sincronizaciones siguientes solo envían los documentos modificados y son
mucho más cortas.

## Si ya usabas la integración por API de Kalku

Kalku también puede leer tu Odoo por API (Configuración > Integraciones >
"Odoo"). Si la usabas y pasas a este módulo:

- Primero desconecta esa integración en Kalku: Kalku no deja tener las dos
  conectadas a la vez.
- La primera sincronización del módulo es completa y mueve las ventas que ya
  estaban cargadas en el centro por defecto al centro del almacén de cada
  documento.
- La integración por API contaba los productos sin referencia interna por su
  nombre; el módulo los omite. Cuando el módulo vuelva a enviar esos documentos,
  su demanda desaparece de Kalku. Ponles referencia interna antes de cambiar si
  quieres conservarla.

## Desinstalar

Al desinstalar el módulo se borran todas sus claves `kalku.*` de
`ir.config_parameter`, el token incluido. Revoca también el token en Kalku
(Configuración > Integraciones > "Odoo (módulo)" > "Revocar token").

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
