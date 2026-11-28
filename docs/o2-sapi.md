# La API de O2 Cloud (SAPI) tal y como la usa funambridge

O2 Cloud (Telefónica España) es un despliegue *white-label* de **Funambol
OneMediaHub**. Su API, **SAPI**, no está documentada públicamente. Esta
especificación recoge lo que funambridge sabe de ella, obtenido de capturas
HAR de la web oficial, de pruebas en vivo contra una cuenta propia y, donde se
indica, de otro proyecto. Refleja el estado de **noviembre de 2026**, y O2 la
cambia sin avisar (ver *Historial de cambios de O2*).

Marcas usadas:

- ✔ comprobado en vivo por funambridge;
- ◐ visto en capturas o de forma parcial;
- ↗ lo documenta otro proyecto
  ([MovistarWebDAVProxy](https://github.com/uniextra/MovistarWebDAVProxy),
  Movistar Cloud / O2 Cloud); no lo hemos verificado.

## 1. Hosts

| Host | Para qué |
|---|---|
| `cloud.o2online.es` | Web y SAPI (`/sapi/...`), descargas firmadas (`/sapi/download/...`) ✔ |
| `upload.cloud.o2online.es` | Subidas (`/sapi/upload`) ✔. También lo anuncia `system/information` → `sapi.upload.endpoint` |
| `apiseg.telefonica.es` | Servidor OAuth2/OpenID de Telefónica (authorize/token) ✔ |
| `t3.o2online.es` | Página de login (número + SMS) ✔ |
| `api.telefonica.es` | API de credenciales que usa esa página (`/t3/cus/segu/v5/seguCredentialO2s/...`) ✔ |

Todo va detrás de **CloudFront**, que mira la "huella" del *handshake* TLS.
Con un contexto TLS sin ALPN (`ssl.create_default_context()` de Python en
Linux), CloudFront responde **401 con cuerpo vacío** a todo. Con el contexto de
`urllib` (ALPN `http/1.1` y *post-handshake auth*) funciona ✔. Desde Windows,
ambos funcionaban.

Lo que cuenta es el **ALPN** (que el saludo TLS diga qué protocolo quiere) ✔
(28‑nov‑2026, prueba `go test -tags live -run LiveTLSVariants`): con el TLS
estándar de Go, anunciando `http/1.1` o con el cliente por defecto (HTTP/2),
todo funciona (sesión, listados, subida, descarga); **sin ALPN, CloudFront
responde 401 vacío** a todo, incluso con una sesión válida. La versión 1.x (Go) presenta el ClientHello de **Chrome**
(uTLS) con ALPN reducido a `http/1.1` ✔: con el ALPN de Chrome tal cual
(`h2,http/1.1`) CloudFront negocia HTTP/2.

## 2. Sesión y autenticación

### 2.1 Qué identifica una sesión

Cada llamada a SAPI lleva:

| Dónde | Qué |
|---|---|
| Cookie `JSESSIONID` | la sesión web (HttpOnly, `Max-Age=86400`, el servidor la reenvía en respuestas) ✔ |
| Cookie `validationKey` | token anti-CSRF ✔ |
| Query `validationkey=<mismo valor>` | el mismo token repetido en la URL (*double submit*) ✔ |
| Cabecera `X-deviceid` | el dispositivo al que pertenece la sesión ✔ (ver 2.3) |

Otras cabeceras que envía funambridge: `User-Agent`, `Accept: application/json`
y `X-request-id: <uuid hex>`.

La cookie `PLC` ("no cerrar sesión") ya no la entrega el login web de O2 en
noviembre de 2026 ◐. Aun así, `system/information` dice
`sapi.login.persistent-login-cookie.enabled: true`: queda como pista sin
investigar para conseguir sesiones largas.

### 2.2 Rotación de la `validationKey` (SEC-1003) ✔

O2 cambia la clave periódicamente; en los logs se ve una vez al día, más o
menos. La siguiente llamada falla con HTTP 401, o a veces HTTP 200, y este
cuerpo:

```json
{"error": {"code": "SEC-1003",
           "message": "Invalid mandatory validation key ...",
           "data": "<la validationKey nueva>"}}
```

El cliente debe adoptar `error.data` como nueva clave (en el parámetro de la
URL y en la cookie) y repetir la llamada. La sesión sigue viva. Algunas
respuestas correctas también traen `data.validationkey` o una cookie
`validationKey` nueva, y hay que quedarse con ellas.

> ⚠️ **Nunca envíes una `validationkey` inventada o incorrecta.** Una sola
> petición con `validationkey=000…` **invalidó la sesión entera** (28‑nov‑2026)
> ✔. A partir de ahí, todo devolvía la página de login, también al resto de
> clientes que compartían la sesión. Una clave *rotada* (SEC-1003) no la invalida;
> una *falsa*, sí.

### 2.3 Dispositivo (`X-deviceid`) ✔

- La sesión queda ligada al `deviceid` con el que se hizo el login (paso 2 de
  la sección 3). La web usa `web-<hex>` y funambridge genera el suyo con el
  mismo formato.
- **El host de subidas exige** que `X-deviceid` sea ese dispositivo. Si no lo
  es, responde **401 con cuerpo vacío**. Las lecturas en `cloud.o2online.es` no
  lo comprueban.

### 2.4 Caducidad ✔

- **Por inactividad**: desde noviembre de 2026, la sesión muere tras unas 1–2 h
  **sin peticiones**; antes aguantaba días. La web hace consultas cada minuto
  más o menos y por eso no se nota. funambridge llama a `profile?action=get`
  cada `keepalive_minutes` (5).
- **Duración máxima**: no la conocemos para O2. ↗ En Movistar Cloud la sesión
  web caduca a las 24 h aunque haya actividad.
- Una sesión caducada responde **HTTP 401 con una página HTML** (la del login),
  no JSON.

### 2.5 Cómo distinguir los 401

| Respuesta | Significado |
|---|---|
| 401/200 con JSON `SEC-1003` + `data` | clave rotada: adoptarla y reintentar |
| 401 con HTML | sesión caducada o invalidada: hay que volver a hacer login |
| 401 vacío desde `upload.*` | `X-deviceid` no es el de la sesión |
| 401 vacío en cualquier host, incluso tras un login recién hecho | CloudFront rechaza la conexión (huella TLS, ver §1) |

### 2.6 Otros modos de autenticación (no usados ahora)

- **Token `Authorization: oauth <base64>`** de la app Android:
  `{"data":{"accesstoken","refreshtoken","platform","expiresin","lastrefreshdate","msisdn"}}`
  en Base64, más `POST /sapi/login?action=login`. El servidor devuelve los tokens
  rotados en la cabecera `Authorization` de la respuesta. Obtener el token exige
  la red móvil. Queda en el código, pero no se usa.
- ↗ **OAuth de la app de Windows**: `client_id` de escritorio de O2,
  `redirect_uri=https://cloud.o2online.es/ui/html/clientoauth.html`. El código
  se canjea por `access_token` + `refresh_token` en
  `apiseg.telefonica.es/.../o2/cus/token`, y después se hace
  `GET /sapi/login/oauth?action=login` con `Authorization: oauth …` (plataforma
  `windows`). Así la sesión se recupera sin SMS.

## 3. Login con número + SMS (OAuth2 + PKCE de la web) ✔

Es el flujo que recorre `funambridge/login.py`. Pasos 1, 2 y 5 con el mismo
*cookie jar*, y sin seguir las redirecciones automáticamente:

1. `GET https://cloud.o2online.es/` → `JSESSIONID` previo, donde O2 guarda el
   *PKCE verifier* de este login.
2. `GET /sapi/oauth/pkce/authorize?platform=web&deviceid=<web-…>` → `302` a
   `apiseg.telefonica.es/openid/connect/auth/oauth/v2/o2/cus/authorize?...`
   (`client_id` de la web, `O2CLOUD_WEB`, PKCE S256) → `302` a
   `https://t3.o2online.es/acceso/#/accessUserPassO2?sessionID=…&sessionData=…&state=…`.
   Los parámetros van en el **fragmento** (`#`). Hay que leerlos de la cabecera
   `Location` y decodificarlos como `decodeURIComponent`: un `+` sigue siendo
   `+`.
3. `POST https://api.telefonica.es/t3/cus/segu/v5/seguCredentialO2s/manageCredentialMobileO2`

   ```json
   {"mobile": "<9 cifras>", "sessionID": "…", "sessionData": "…", "consumerId": "O2CLOUD_WEB"}
   ```

   → `{"newSessionData": "…", "newSessionID": "…"}` y **O2 envía el SMS**.
4. `POST …/verifyCredentialMobileO2`

   ```json
   {"otp": "<código>", "sessionData": "<newSessionData>", "sessionID": "<newSessionID>", "Mobile": "<9 cifras>"}
   ```

   → `{"redirectUri": "https://cloud.o2online.es/sapi/login/oauth?code=…&state=…"}`
5. `GET <redirectUri>` con el jar del paso 1 → `Set-Cookie: JSESSIONID=…` y
   `validationKey=…`. La sesión ya está lista y pertenece al `deviceid` del
   paso 2.

En los pasos 3 y 4 la API de Telefónica **no usa cookies**: todo el estado viaja
en `sessionID`/`sessionData`. Cabeceras obligatorias:

| Cabecera | Valor |
|---|---|
| `Content-Type` | `application/json` |
| `Origin` / `Referer` | `https://t3.o2online.es` |
| `COCO.idOrigen` | `O2CLOUD_WEB/loginAppO2` |
| `COCO.idGeneral` | UUID, el mismo en todo el login |
| `COCO.idProceso`, `COCO.idMensaje` | UUID nuevo en cada petición |

En noviembre de 2026 no había captcha.

## 4. Convenciones de SAPI

- URL: `https://cloud.o2online.es/sapi/<recurso>?action=<acción>&validationkey=…[&parámetros]`.
- Respuesta correcta: `{"data": {...}, "responsetime": <ms epoch>}` ✔.
- Respuesta de error: `{"error": {"code": "XXX-0000", "message": "…", "data": …}}`.
  Puede llegar **con HTTP 200**, así que hay que mirar siempre el cuerpo ✔.
- Fechas de ficheros: milisegundos desde epoch (enteros) ✔.
- Los ids son enteros: en las URL van como texto; en los cuerpos JSON, como
  número.

## 5. Endpoints

### 5.1 Información del sistema (pública) ✔

`GET /sapi/system/information?action=get`: **no necesita sesión**. Es lo que lee
la web al arrancar. Campos útiles:

| Campo | Valor (nov. 2026) | Uso |
|---|---|---|
| `sapi.upload.endpoint` | `https://upload.cloud.o2online.es` | host de subidas |
| `sapi.upload.max-size-in-mb` | `4096` | tamaño máximo por fichero |
| `folders.softdelete-updates-limit` | `1000` | elementos máx. al borrar una carpeta (ver FOL-1036) |
| `folders.move-updates-limit` | `1000` | ídem al mover |
| `trash.cleanup.items-expire-time-in-minutes` | `86400` | la papelera se vacía sola a los 60 días |
| `downloadurl` | `https://cloud.o2online.es/d` | base de descargas |
| `logininfo.oauthinfo` | URLs de authorize/token y `client_id` de la web | — |
| `sapi.login.persistent-login-cookie.enabled` | `true` | ver 2.1 |
| `mediainfo.supportedtypes` | extensiones de foto/vídeo/audio | O2 procesa esos ficheros como multimedia |

### 5.2 Perfil ✔

`GET /sapi/profile?action=get` → `data.user` con `generic`, `phones[]`,
`roles[]`, `invitations[]` y `datatakeout`. funambridge lo usa para comprobar
que la sesión vive y como *keepalive*.

### 5.3 Carpetas

| Operación | Petición | Respuesta |
|---|---|---|
| Raíz ✔ | `GET /sapi/media/folder/root?action=get` | `data.folders[0]` = la carpeta raíz (`id`) |
| Listar ✔ | `GET /sapi/media/folder?action=get&parentid=<id>` | `data.folders[]` |
| Crear ◐ | `POST /sapi/media/folder?action=save` con `{"data":{"name":"…","parentid":<id>}}` | el id llega con formas distintas: `data.id`, `data.folder.id` o `data.folders[0].id`; si no llega, se vuelve a listar y se busca por nombre |
| Borrar ✔ | `GET /sapi/media/folder?action=softdelete&id=<id>` | a la papelera |

- **`parentid` se ignora**: el listado devuelve **todas** las carpetas de la
  cuenta (una lista plana, raíz incluida). Hay que filtrar por el campo
  `parentid` de cada carpeta ✔.
- Cada carpeta trae, al menos, `id`, `name` y `parentid` ◐.
- **FOL-1036** ✔ (código) ↗ (observado): no se puede borrar o mover de una vez
  una carpeta con más elementos que `folders.softdelete-updates-limit`. Hay que
  vaciarla antes (ficheros uno a uno, subcarpetas recursivamente).

### 5.4 Ficheros (media)

**Listar una carpeta** ✔:

```
POST /sapi/media?action=get&folderid=<id>&limit=<n>&offset=<m>
{"data": {"fields": ["name","size","modificationdate","creationdate","etag"]}}
```

→ `{"data": {"media": [...], "more": true|false}}`. Se pagina con `offset`
mientras `more` sea `true`. funambridge usa `limit=200`; una carpeta con 4.000
ficheros son 20 llamadas.

- Cada elemento trae: `id`, `name`, `size` (bytes), `creationdate`,
  `modificationdate`, `etag`, `status`, `mediatype`, `userid` y `date` ✔.
- Nombres de campo válidos en `fields`: `name`, `size`, `modificationdate`,
  `creationdate`, `etag`, `url`, `folderid` ◐. **`mime` y `contenttype` no son
  válidos**: el tipo se deduce del nombre.
- Un `GET` sin cuerpo devuelve solo `id`/`date` (un resumen para sincronizar) ◐.
- **El `etag` NO es el MD5 del contenido** ✔. Para comparar copias hay que
  descargarlas.
- Puede haber **varios ficheros con el mismo nombre** en una carpeta (ver 5.6).

**Pedir la URL de descarga** ✔:

```
POST /sapi/media?action=get
{"data": {"ids": [<id numérico>], "fields": ["url"]}}
```

→ `data.media[0].url` = `/sapi/download/…?action=…&k=…&node=…`, una URL
firmada; puede ser relativa. Un id inexistente **no da error**: devuelve la lista
vacía ✔.

**Descargar** ✔: `GET <url firmada>` con las cookies de la sesión.

- Admite **`Range`**: responde `206` + `Content-Range`, sin redirecciones.
  No trae `Content-Length`: el cuerpo va troceado (*chunked*).
- **Los rangos "desde el final" (`bytes=-N`) los resuelve mal**: devuelve
  `bytes 0-N/…`. Hay que pedir siempre rangos absolutos.
- La URL se puede reutilizar durante un tiempo; funambridge la guarda 20 min.
  Si falla con 401/403/404, se pide otra.
- ↗ Un fichero recién subido todavía en validación devuelve `COM-1005` hasta
  que O2 termina de procesarlo.

**Borrar** ✔: `GET /sapi/media?action=softdelete&id=<id>` (va a la papelera).

**Espacio** ✔: `GET /sapi/media?action=get-storage-space&softdeleted=true` →

```json
{"data": {"quota": 10995116277760, "used": …, "free": …, "softdeleted": …,
          "nolimit": false, "individual": {"used": …, "softdeleted": …}}}
```

En bytes. Lo que hay en la papelera (`softdeleted`) **no** cuenta en `used`, y
`free` = `quota` − `used`.

### 5.5 Subir un fichero

Hay dos protocolos y **los dos funcionan con la sesión web** (la del login por
SMS), siempre que `X-deviceid` sea el dispositivo de la sesión ✔ (28‑nov‑2026).

#### a) El de la app de Windows, en dos pasos (lo usa la 1.x) ✔

1. **Registrar** el fichero (en el host web):

   ```
   POST /sapi/upload/<tipo>?action=save-metadata&responsetime=true&lastupdate=true
   Content-Type: application/octet-stream
   {"data":{"contenttype":"application/octet-stream","creationdate":"20261128T130000Z",
            "modificationdate":"20261128T130000Z","folderid":<id>,"name":"f.bin","size":<bytes>}}
   ```

   → `{"success":"Media metadata saved successfully","id":"<id>","lastupdate":…}`.
   `<tipo>` es `file`, `picture`, `video` o `audio` (según la extensión; un
   contenido que no encaja da `MED-1050` y se puede subir como `file`). Las
   fechas van como `AAAAMMDDTHHMMSSZ`.
   **Con `"id": "<id existente>"` sobrescribe ese fichero conservando su id** ✔:
   así nunca aparece `nombre (1).ext`. Con `"id"` y solo `name`/`folderid`,
   **renombra o mueve** sin volver a subir ✔.
2. **Enviar el contenido** (host de subidas), el fichero en bruto:

   ```
   POST https://upload.cloud.o2online.es/sapi/upload/<tipo>?action=save&lastupdate=true&acceptasynchronous=true
   Content-Type: application/octet-stream
   x-funambol-id: <id>
   x-funambol-file-size: <bytes>
   ```

   → `{"success":"Media uploaded successfully","id","lastupdate","status":"V","etag"}`.
   Funciona también con 0 bytes ✔ (fichero nuevo; ↗ sobrescribir con 0 bytes da
   `MED-1049` y el original se pierde: hay que borrar y crear).
3. **Reanudar** si el envío se corta ✔: preguntar cuánto tiene O2

   ```
   GET /sapi/upload/<tipo>?action=save&lastupdate=true&acceptasynchronous=true   (host web)
   content-range: bytes */<total>
   x-funambol-id: <id>
   x-funambol-file-size: <total>
   ```

   → `308` + `Range: 0-<último byte recibido>` (el cuerpo trae HTML; se ignora),
   o `200` con `etag`/`status` si ya lo tenía completo. Después se envía solo el
   resto, igual que en el paso 2, con `content-range: bytes <desde>-<total-1>/<total>`.

Las respuestas de subida **no van envueltas en `data`**: los campos están en el
primer nivel ✔. El `etag` no es el MD5 (ni en hex ni en base64) ✔.

Un fichero **registrado sin contenido es invisible** ✔ (28‑nov‑2026): no sale al
listar la carpeta ni pidiéndolo por su id. Si se pierde la respuesta del paso 1
y se registra otra vez el mismo nombre, el registro huérfano no estorba: el
nuevo conserva el nombre (no pasa a `nombre (1)`) y el huérfano, que nunca
recibe contenido, no aparece. Por eso el paso 1 se puede reintentar.

#### b) El de la web, en una petición *multipart* ✔

```
POST https://upload.cloud.o2online.es/sapi/upload?action=save&validationkey=…
Content-Type: multipart/form-data; boundary=…

parte "data": {"data":{"name":"f.bin","size":<bytes>,"modificationdate":"","folderid":<id>}}
parte "file": los bytes (application/octet-stream)
```

→ `{"success","id","status":"V","etag","type"}`. No sobrescribe ni se reanuda.

#### Después de subir

- `status: "V"` = validando. **O2 procesa el fichero en segundo plano** (unos
  segundos; hasta ~30 s): mientras tanto puede no aparecer en los listados ni
  descargarse (↗ `COM-1005`). Cuándo termina se sabe con:

  ```
  POST /sapi/media?action=get-validation-status
  {"data":{"ids":[{"id":<id>,"folder_id":<carpeta>}]}}
  ```

  → `{"data":{"ids":[{"id":<id>,"status":"V"}]}}` mientras valida; después otra
  letra (`U` en las pruebas, a los ~6 s) ✔. Un id que O2 ya no conoce no
  aparece en la lista.
- **O2 nunca sobrescribe por nombre**: si se crea (sin `"id"`) un fichero con un
  nombre que ya existe, guarda el nuevo como `nombre (1).ext`…
- Si la conexión se corta **después** de enviar los bytes, O2 puede haber
  guardado el fichero igualmente; con el protocolo a) se ve con la consulta de
  reanudación.
- Tras enviar el último byte, O2 puede tardar minutos en responder en ficheros
  grandes (↗ 87 s con 153 MB).
- Tamaño máximo: `sapi.upload.max-size-in-mb` (4 GB). ↗ Por encima, `MED-1020`.

### 5.6 Copiar en la nube ✔

```
POST /sapi/media/file?action=copy
{"data":{"id":<id>,"parentid":<carpeta destino>,"name":"copia.bin"}}
```

→ el id de la copia (en `data.id`, o dentro de `data.media`/`data.file`). Sin
descargar ni volver a subir. La copia también pasa por validación.

### 5.7 Renombrar o mover una carpeta ↗

`POST /sapi/media/folder?action=save` con `{"data":{"id":<id>,"name":"…","parentid":<nuevo padre>}}`.

## 6. Códigos de error conocidos

| Código | Significado | Fuente |
|---|---|---|
| `SEC-1003` | validationKey rotada; la nueva va en `error.data` | ✔ |
| `FOL-1035` | límite de carpetas de la cuenta (la papelera cuenta) | ↗ |
| `FOL-1036` | demasiados elementos para borrar o mover una carpeta de una vez | ↗ |
| `MED-1017` | el fichero aún se está validando y no se puede tocar | ↗ |
| `MED-1020` | fichero demasiado grande | ↗ |
| `MED-1048` | rechazo por tamaño durante la subida | ↗ |
| `MED-1049` | sobrescribir con 0 bytes (el original se pierde) | ↗ |
| `MED-1050` | contenido no admitido para su tipo (p. ej. un `.jpg` que no es una imagen) | ↗ |
| `MED-1000` | error genérico del servidor de medios | ↗ |
| `COM-1005` | descarga de un fichero aún no procesado | ↗ |

## 7. Cómo lo usa funambridge (WebDAV → SAPI)

Versión 1.x (Go):

| WebDAV | SAPI |
|---|---|
| `PROPFIND` | carpetas: `folder?action=get` (el árbol entero en una llamada, cacheado 3 min y releído si falta algo); ficheros: `media?action=get&folderid=…` (una vez por carpeta, luego índice en memoria); cuota: `get-storage-space` (caché de 60 s) |
| `HEAD` | solo el índice, sin llamar a O2 |
| `GET` (con o sin `Range`) | lo recién subido, desde la caché de escritura durante `cache_seconds`; si no, y el fichero aún se valida, espera (`get-validation-status`); `media?action=get` (URL, caché de 20 min) + `GET <url>` con `Range` absoluto, en streaming (se corta si O2 pasa 60 s sin enviar nada) |
| `PUT` | protocolo a) de 5.5: sobrescribe con `"id"` el fichero existente, reanuda cortes (hasta 8 intentos), retira el fichero a medias si falla; borra copias de más con el mismo nombre; sigue la validación |
| `MOVE` | ficheros: `save-metadata` con `id` + `name`/`folderid`; carpetas: `folder?action=save` con `id` |
| `COPY` | ficheros: `media/file?action=copy` |
| `DELETE` | `media?action=softdelete` (todas las copias) / `folder?action=softdelete` (por partes si `FOL-1036`) |
| `MKCOL` | `folder?action=save` |
| *keepalive* | `profile?action=get` cada 5 min |
| límites | `system/information` (caché de 6 h) |

Transporte: uTLS con la huella de Chrome y HTTP/1.1, conexiones reutilizadas
(las subidas, sin reutilizar), `TCP_USER_TIMEOUT` de 60 s en Linux.

La 0.2.x (Python) usaba la subida multipart (b) y borraba antes de subir; la
caché de escritura es la misma idea.

## 8. Historial de cambios de O2

| Fecha | Cambio | Efecto |
|---|---|---|
| ~19‑nov‑2026 | Subida nueva multipart en `upload.*`, que exige `X-deviceid`; la de dos pasos da 401 vacío con sesión web | 0.1.8 |
| nov‑2026 | Login web por OAuth2 + PKCE en `t3.o2online.es`; sin cookie `PLC` | 0.1.9 (login por SMS) |
| nov‑2026 | La sesión muere tras ~1–2 h sin peticiones | 0.1.11 (*keepalive*) |
| nov‑2026 | CloudFront rechaza clientes TLS sin ALPN (visto desde Docker/Linux) | 0.2.1 |
| 28‑nov‑2026 | Comprobado que la sesión web admite el protocolo de subida de la app de Windows (sobrescribir, reanudar, validación) | 1.0.0-beta.1 (Go) |
