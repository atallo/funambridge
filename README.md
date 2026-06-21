# funambridge

Accede a una o varias cuentas de **O2 Cloud** (`cloud.o2online.es`) por
**S3** y **WebDAV**, desde Linux, Docker o donde quieras.

## Qué es O2 Cloud

O2 Cloud (Telefónica España) solo tiene clientes para Windows/macOS/Android/iOS,
**sin cliente Linux** y **sin S3 ni WebDAV nativos**. Es un despliegue
*white-label* de **Funambol OneMediaHub**, con una API propietaria (*SAPI*) y un
login **MobileConnect** (número de teléfono + SMS). `funambridge` (Funambol +
*bridge*) hace de puente hacia S3 y WebDAV.

## Cómo funciona

Un único puerto sirve tres cosas, distinguidas por la petición:

- **API S3** — peticiones con firma AWS SigV4.
- **WebDAV** — peticiones con auth HTTP Basic (o métodos WebDAV).
- **Panel web** (`/_admin/`) — un navegador.

Autenticación: cada cuenta tiene `access_key` + `secret_key`. **Ambas deben ser
correctas.** S3 verifica la firma SigV4; WebDAV usa Basic con usuario =
`access_key` y contraseña = `secret_key`. Por cuenta puedes activar S3 y/o WebDAV.

La sesión de O2 se captura de un login web normal (ver *Añadir una cuenta*); las
llamadas a SAPI la reutilizan (cookies `JSESSIONID` + `validationKey` + `PLC`).

## Ejecutar

Local:

```bash
pip install -r requirements.txt        # solo PyYAML
python -m funambridge serve            # http://0.0.0.0:9000  (panel en /_admin/)
```

Docker:

```bash
docker compose up -d                   # sirve :9000, config en ./data/config.yaml
# o:
docker build -t funambridge .
docker run -d -p 9000:9000 -v "$PWD/data:/data" funambridge
```

Pon tu propio **proxy HTTPS** delante para TLS. Debe reenviar
`X-Forwarded-Proto` y `X-Forwarded-Host` (para mostrar el endpoint y para que el
Host de la firma SigV4 coincida). Si tu proxy no puede, pon
`verify_s3_signature: false` en `config.yaml`.

Si un cliente S3/WebDAV exige HTTPS y no tienes proxy delante, usa el **TLS
integrado**: `serve --tls-cert cert.pem --tls-key key.pem` (o la sección `tls`
del config). Si los ficheros no existen, se generan autofirmados — marca
"aceptar certificado no fiable" en el cliente.

## Añadir una cuenta

Abre el panel (`http://<host>:9000/_admin/`) → botón **«＋ Añadir cuenta nueva»**.
Tres formas:

1. **Subir un HAR completo** (recomendado). En DevTools → Network, activa la
   exportación de HAR **completo** (con datos sensibles, no «sanitized», que
   borra las cookies), inicia sesión en `cloud.o2online.es` (teléfono + SMS),
   guarda todo como HAR y súbelo.
2. **Pegar las cookies**: `JSESSIONID`, `validationKey` y `PLC` (DevTools →
   Application/Almacenamiento → Cookies → `cloud.o2online.es`).
3. **Snippet de consola**: útil desde un navegador en el **móvil** (en escritorio
   el flujo MobileConnect de la app suele fallar porque exige la red móvil).

Cada cuenta recibe `access_key`/`secret_key` automáticas; puedes regenerarlas o
fijarlas desde el panel. Para refrescar la sesión sin perder las claves, usa
**«Renovar sesión»** en la fila de la cuenta.

## Conectar

Un endpoint: `http://<host>:9000` (o vía tu proxy HTTPS).

- **WebDAV**: cualquier cliente WebDAV — usuario = `access_key`, contraseña =
  `secret_key`.
- **S3**: cualquier cliente S3, **path-style**, región ignorada, con
  `access_key`/`secret_key`. En el campo *Host* va **solo el host** (sin
  `http://` ni el puerto); el **puerto** va en su casilla; **TLS** según uses
  HTTP local o tu proxy HTTPS.

## Mapeo S3 / WebDAV ⇄ O2

| Concepto             | O2 Cloud / OneMediaHub                          |
|----------------------|-------------------------------------------------|
| Cuenta               | se elige por `access_key`                        |
| Carpeta / bucket     | una carpeta de OneMediaHub (buckets = raíz)      |
| Fichero              | un media item (`POST /sapi/media?action=get`)    |
| Descargar            | url pre-firmada del item                         |
| Subir                | save-metadata + binario en streaming (`/sapi/upload/file`) |
| Borrar               | soft-delete (papelera)                           |

S3: ListBuckets, Create/Delete/HeadBucket, ListObjects(V2), Get/Head/Put/Delete
(con Range y aws-chunked). WebDAV: PROPFIND, GET/HEAD (Range), PUT, DELETE,
MKCOL, OPTIONS, LOCK/UNLOCK (simulados). La raíz lista carpetas **y** ficheros.

## Limitaciones

- **Caducidad de la sesión.** La sesión web caduca con el tiempo; cuando empiece
  a dar 403, usa «Renovar sesión» con un HAR/cookies frescos. (El token OAuth
  duradero de la app Android exige la red móvil, no se obtiene en escritorio.)
- **Latencia al subir.** Tras subir, O2 procesa el item de forma asíncrona (unos
  segundos a ~30 s) antes de que aparezca en los listados/descargas. La caché de
  escritura (`cache_seconds` por cuenta) tapa esa ventana sirviendo el fichero
  recién subido: hasta 256 MB en memoria y el resto volcado a disco
  (`cache_dir`), que se sirve en streaming. Sube `cache_seconds` por encima de la
  peor latencia de O2 (p. ej. 45) si lees justo después de escribir.
- **Subida y descarga en streaming**: ni subir ni bajar cargan el fichero entero
  en memoria (un `PUT` se lee a RAM hasta ~16 MB y a partir de ahí se vuelca a
  disco). Excepción: un `PUT` S3 con cuerpo `aws-chunked` (firma *streaming*) se
  decodifica en memoria; usa WebDAV o `UNSIGNED-PAYLOAD` para ficheros enormes.
- Sin subida multiparte S3, sin renombrar/MOVE/COPY (de momento). Los borrados
  son soft-delete.

## Estructura

| Fichero                   | Función                                      |
|---------------------------|----------------------------------------------|
| `funambridge/cli.py`      | CLI: `serve` / `add` / `import-har` / `probe`|
| `funambridge/config.py`   | config YAML, cuentas, claves, flags          |
| `funambridge/auth.py`     | verificación de firma S3 (SigV4)             |
| `funambridge/sapi.py`     | cliente de la API SAPI de Funambol           |
| `funambridge/store.py`    | mapeo ruta ⇄ SAPI                            |
| `funambridge/s3.py`       | front-end HTTP: enruta S3 / WebDAV / panel    |
| `funambridge/webdav.py`   | front-end WebDAV                              |
| `funambridge/har.py`      | extrae la sesión de un HAR                    |
| `funambridge/admin.py`    | panel web de administración                   |

## Aviso

Uso personal contra tu propia cuenta de O2 Cloud. `config.yaml` contiene tu
sesión activa: trátalo como una contraseña (está excluido de git).
