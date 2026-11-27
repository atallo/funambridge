# funambridge

Accede a una o varias cuentas de **O2 Cloud** (`cloud.o2online.es`) por
**WebDAV**, desde Linux, Docker o donde quieras. Pensado sobre todo como
destino de **Synology Hyper Backup**, pero vale para cualquier cliente WebDAV
(rclone, WinSCP, Cyberduck, unidades de red de Windows/macOS/Linux).

## Qué es O2 Cloud

O2 Cloud (Telefónica España) solo tiene clientes para Windows/macOS/Android/iOS,
**sin cliente Linux** y **sin WebDAV**. Es un despliegue *white-label* de
**Funambol OneMediaHub**, con una API propietaria (*SAPI*) y un login por número
de teléfono + SMS. `funambridge` (Funambol + *bridge*) hace de puente.

## Cómo funciona

Un único puerto sirve dos cosas:

- **WebDAV** — cualquier ruta salvo `/_admin` (también acepta `/dav/...`).
- **Panel web** (`/_admin/`) — para añadir cuentas, iniciar sesión por SMS y
  explorar los ficheros.

Cada cuenta tiene su **usuario y contraseña WebDAV** (autenticación HTTP Basic),
generados al crearla; se pueden regenerar o fijar desde el panel. El usuario
elige la cuenta de O2.

La sesión de O2 se obtiene con un login normal (ver *Añadir una cuenta*); el
proxy la reutiliza y la mantiene viva.

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

Pon tu propio **proxy HTTPS** delante si lo vas a usar fuera de tu red. Si un
cliente exige HTTPS y no tienes proxy, usa el **TLS integrado**:
`serve --tls-cert cert.pem --tls-key key.pem` (o la sección `tls` del config);
si los ficheros no existen se generan autofirmados.

## Añadir una cuenta

Abre el panel (`http://<host>:9000/_admin/`) → **«＋ Añadir cuenta nueva»**:

1. **Con tu número (SMS)** (recomendado). Escribe un nombre y tu número de O2 y
   pulsa «Enviar SMS»; escribe el código que te llega y listo. El proxy inicia
   sesión en O2 por sí mismo (el mismo flujo OAuth que usa la web), sin HAR ni
   navegador, y la sesión queda asociada a su propio dispositivo.
2. **Subir un HAR completo** de un login en
   [cloud.o2online.es](https://cloud.o2online.es) (DevTools → Red, exportación
   **con** datos sensibles).
3. **Pegar las cookies** `JSESSIONID` y `validationKey` **y el `x-deviceid`**
   (DevTools → Red → cualquier petición `/sapi` → cabeceras).

Para refrescar la sesión sin perder el usuario/contraseña WebDAV, usa
**«Renovar sesión»** en la fila de la cuenta. El panel tiene arriba un enlace
**«Abrir O2 Cloud»** a la web.

O2 solo acepta **subidas** desde el dispositivo al que pertenece la sesión
(`x-deviceid`). Con el SMS y con el HAR se resuelve solo; con cookies pegadas a
mano hay que rellenarlo. Sin él la cuenta puede **leer pero no escribir**, y el
panel lo marca como «solo lectura · renueva la sesión».

## Conectar (Hyper Backup)

Destino **WebDAV**: servidor = la IP/nombre del equipo, puerto `9000` (o el de
tu proxy HTTPS), usuario y contraseña = los de la cuenta en el panel. Cualquier
otro cliente WebDAV: URL `http://<host>:9000`.

## Qué soporta el WebDAV

| Método / función | |
|---|---|
| PROPFIND (Depth 0/1) | tamaño, fechas, `getetag` y la **cuota** (RFC 4331: espacio libre y usado de la cuenta de O2) |
| GET / HEAD | con **rangos**: se piden a O2 solo los bytes solicitados y se reenvían en streaming (nada se carga entero en memoria); HEAD no descarga |
| PUT | en streaming a O2 (`upload.<host>`); los ficheros de más del máximo de O2 (4 GB) se rechazan con `413` antes de recibirlos |
| DELETE / MKCOL | borrar = a la papelera de O2; las carpetas con más elementos de los que O2 deja borrar de una vez se vacían por partes |
| LOCK / UNLOCK | simulados (para clientes que bloquean antes de escribir) |
| Errores de O2 | los pasajeros se devuelven como `503` + `Retry-After`, para que el cliente reintente |

No hay MOVE/COPY/PROPPATCH (Hyper Backup no los usa).

Los límites y el host de subida se leen de `/sapi/system/information` de O2
(tamaño máximo por fichero, elementos por borrado de carpeta, caducidad de la
papelera); el explorador del panel los muestra junto con la cuota.

## Limitaciones y detalles

- **Sesión.** O2 rota periódicamente la *validation key*; el proxy la
  **renueva sola**. Desde nov. 2026, O2 cierra la sesión tras ~1-2 h **sin
  peticiones**: el proxy hace una petición ligera por cuenta cada
  `keepalive_minutes` (5 por defecto). El panel muestra «viva · comprobada …» o
  «caducada · renueva la sesión»; en ese caso usa «Renovar sesión» (SMS).
- **Latencia al subir.** Tras subir, O2 procesa el item de forma asíncrona (de
  unos segundos a ~30 s) antes de que aparezca en los listados/descargas. La
  caché de escritura (`cache_seconds` por cuenta) tapa esa ventana sirviendo el
  fichero recién subido: hasta 256 MB en memoria y el resto en disco
  (`cache_dir`).
- **O2 no sobrescribe**: si subes un nombre que ya existe, guarda otra copia como
  `nombre (1).ext`. El proxy borra la copia anterior antes de subir y, si una
  subida falla a medias (timeout), busca y borra después la copia que O2
  pudiera haber guardado igualmente.
- **Conexiones**: las llamadas a O2 reutilizan la conexión (keep-alive) en vez
  de abrir una por petición; en copias grandes eso evitaba quedarse sin puertos
  (`[Errno 99] Cannot assign requested address`).
- Un `PUT` sin `Content-Length` (cuerpo *chunked*) se rechaza con `411`.

## Reparar duplicados

Versiones anteriores a la 0.1.10 podían dejar copias de más tras subidas
fallidas (el mismo nombre dos veces, o `nombre (N).ext`), y eso hace fallar la
comprobación de integridad de Hyper Backup:

```bash
python -m funambridge dedupe backup/MiNAS.hbk --account prueba          # simulación
python -m funambridge dedupe backup/MiNAS.hbk --account prueba --apply  # repara
```

Descarga todas las copias de cada grupo y compara su contenido: si son
idénticas deja una con el nombre original y manda el resto a la papelera; si
solo está completa una copia `(N)`, la vuelve a subir con el nombre original; si
hay copias completas distintas no toca nada y lo indica. Tras reparar,
reinicia el contenedor (el proxy guarda un índice de cada carpeta).

## Configuración

`config.yaml` (ver `config.example.yaml`); casi todo se gestiona desde el
panel. Las configuraciones de versiones anteriores (con `s3:`) se siguen
leyendo; al guardar se reescriben con `server:`.

## Estructura

| Fichero                   | Función                                      |
|---------------------------|----------------------------------------------|
| `funambridge/cli.py`      | CLI: `serve` / `import-har` / `probe` / `dedupe` |
| `funambridge/config.py`   | config YAML, cuentas, usuario/contraseña     |
| `funambridge/sapi.py`     | cliente de la API SAPI de Funambol           |
| `funambridge/store.py`    | mapeo ruta ⇄ SAPI, índice y caché de escritura |
| `funambridge/server.py`   | servidor HTTP: WebDAV + panel, keepalive      |
| `funambridge/webdav.py`   | front-end WebDAV                              |
| `funambridge/login.py`    | login con número + SMS (OAuth/PKCE de O2)     |
| `funambridge/har.py`      | extrae la sesión de un HAR                    |
| `funambridge/repair.py`   | detecta y repara copias duplicadas            |
| `funambridge/admin.py`    | panel web de administración                   |

## Aviso

Uso personal contra tu propia cuenta de O2 Cloud. `config.yaml` contiene tu
sesión activa: trátalo como una contraseña (está excluido de git).
