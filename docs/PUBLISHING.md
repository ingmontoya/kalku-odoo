# Publicación en apps.odoo.com

Lista de pasos para publicar `kalku_connector`. Los realiza el propietario del repositorio.

## Antes de empezar

- El repositorio tiene dos ramas de versión: `17.0` y `18.0`. Ambas deben estar subidas a GitHub.
- El módulo es gratuito (precio 0) y su licencia es LGPL-3.

## Registro del repositorio

1. Entrar en https://apps.odoo.com con la cuenta de publicación e ir a "Mis aplicaciones" > "Registrar repositorio".
2. Indicar la URL del repositorio de GitHub.
3. apps.odoo.com muestra una llave de despliegue (deploy key). Copiarla.
4. En GitHub: Settings > Deploy keys > Add deploy key. Pegar la llave, con acceso de solo lectura.
5. Volver a apps.odoo.com y confirmar el registro.
6. Seleccionar las ramas `17.0` y `18.0`.

## Ficha de cada versión

- Precio: gratuito.
- Licencia: LGPL-3.
- Descripción y capturas: tomadas de `static/description/index.html`.
- Comprobar que la versión del manifiesto es `17.0.1.0.0` en la rama `17.0` y `18.0.1.0.0` en la rama `18.0`.

## Verificación final

- Abrir el enlace de la ficha que se incluye en el mensaje del grupo y comprobar que carga.
- Comprobar que aparecen las dos versiones (17.0 y 18.0) y que se puede descargar cada una.
- Si el enlace no resuelve, revisar que la llave de despliegue esté activa y que las ramas estén registradas.
