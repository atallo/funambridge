# funambridge

Accede a una o varias cuentas de **O2 Cloud** (`cloud.o2online.es`) por
**WebDAV**, desde Linux, Docker o donde quieras. Pensado sobre todo como
destino de **Synology Hyper Backup**, pero vale para cualquier cliente WebDAV
(rclone, WinSCP, Cyberduck, unidades de red de Windows/macOS/Linux).

> **Versión 1.x en Go** (este directorio). La versión anterior en Python (0.x)
> sigue en [`legacy/python`](legacy/python) y su imagen en la etiqueta Docker
> `0.2`. Mientras la 1.x esté en pruebas se publica como `beta`; `latest` sigue
> siendo la 0.x hasta que la 1.x se dé por buena.

## Qué es O2 Cloud

O2 Cloud (Telefónica España) solo tiene clientes para Windows/macOS/Android/iOS,
**sin cliente Linux** y **sin WebDAV**. Es un despliegue *white-label* de
**Funambol OneMediaHub**, con una API propietaria (*SAPI*) y un login por número
de teléfono + SMS. `funambridge` (Funambol + *bridge*) hace de puente. Cómo es
esa API por dentro: [`docs/o2-sapi.md`](docs/o2-sapi.md).

## Ejecutar

Docker (recomendado):

```bash
docker compose up -d            # imagen ghcr.io/atallo/funambridge:beta, puerto 9000
```

o a mano:

```bash
docker run -d --name funambridge -p 9000:9000 -v "$PWD/data:/data" ghcr.io/atallo/funambridge:beta
```

Sin Docker (Go 1.27+):

```bash
go build -o funambridge ./cmd/funambridge
./funambridge --config config.yaml serve          # http://0.0.0.0:9000 (panel en /_admin/)
```

La imagen trae su propio healthcheck (`/funambridge health`). **Si vienes de la
0.x**: tu `config.yaml` vale tal cual (no hay que volver a iniciar sesión), pero
quita del `compose.yaml` un `healthcheck` que llame a `python`: la imagen nueva
no lleva Python.

Para usarlo fuera de tu red, pon un proxy HTTPS delante, o usa el TLS integrado
(`serve --tls-cert cert.pem --tls-key key.pem`; si no existen se generan
autofirmados).

## Añadir una cuenta

Panel (`http://<host>:9000/_admin/`) → **«＋ Añadir cuenta nueva»**:

1. **Con tu número (SMS)** (recomendado): nombre + número → «Enviar SMS» →
   escribe el código. El proxy hace el mismo login OAuth que la web.
2. **Subir un HAR completo** de un login en cloud.o2online.es.
3. **Pegar las cookies** `JSESSIONID` y `validationKey` y el `x-deviceid`.

Cada cuenta tiene su **usuario y contraseña WebDAV**. «Renovar sesión» en su
fila renueva la sesión de O2 sin cambiarlos.

## Conectar

En todos los casos: servidor = la IP o el nombre del equipo donde corre
funambridge, puerto `9000`, y el **usuario y la contraseña de la cuenta** que
muestra el panel. URL para otros clientes: `http://<host>:9000` (también vale
`/dav/...`).

**Synology Hyper Backup**: destino «WebDAV» → servidor, puerto `9000`, usuario,
contraseña y la carpeta de destino.

**WinSCP**: «Nuevo sitio» → protocolo **WebDAV**, cifrado **ninguno** (o
«TLS/SSL implícito» si usas HTTPS), servidor, puerto `9000`, usuario y
contraseña.

**Unidad de red en Windows** (el cliente WebDAV que trae Windows, servicio
«WebClient»). Una sola vez, en PowerShell como administrador:

```powershell
# permitir usuario y contraseña por HTTP (por defecto Windows solo lo admite con HTTPS)
Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\WebClient\Parameters" BasicAuthLevel 2
# subir el límite de tamaño de fichero (por defecto ~50 MB) al máximo (4 GB)
Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\WebClient\Parameters" FileSizeLimitInBytes 0xffffffff
Set-Service WebClient -StartupType Automatic
Restart-Service WebClient
```

Y después, como usuario normal:

```powershell
net use O: http://<host>:9000/ /user:<usuario> <contraseña> /persistent:yes
```

(o en el Explorador: «Este equipo» → «Conectar a unidad de red» → carpeta
`http://<host>:9000/` → marcar «Conectar con otras credenciales»). Microsoft ha
anunciado que retirará el cliente WebDAV de Windows en el futuro; mientras
exista, funciona así.

## Qué hace

**Subidas robustas** (el protocolo de la app oficial de Windows, que también
admite la sesión web):

- el fichero se registra en O2 y luego se envía su contenido; si la conexión se
  corta, se **reanuda** desde el byte que O2 dice tener, sobre el mismo fichero
  (hasta 8 intentos, con esperas de 2 s a 1 min);
- **sobrescribir conserva el mismo fichero** en O2: nunca aparecen copias
  `nombre (1).ext`; si quedaban copias de versiones viejas, se borran;
- si al final falla, el fichero a medias se retira (nunca queda en la nube un
  fichero incompleto);
- el cuerpo del `PUT` se guarda (RAM hasta 16 MB, luego `cache_dir`) para poder
  reanudar sin pedírselo otra vez al cliente;
- los ficheros de más de 4 GB (máximo de O2) se rechazan con `413` antes de
  recibirlos.

**Recién subido**: tras subir, O2 procesa el fichero unos segundos (hasta ~30)
antes de poder descargarse. Durante `cache_seconds` (por cuenta, 15 por defecto;
se cambia en el panel) las lecturas de lo recién subido se sirven desde una
**copia local** (caché de escritura: hasta 256 MB en memoria, el resto en
`cache_dir`), sin esperar. Pasado ese tiempo, si O2 aún no ha terminado, la
lectura **espera** a que acabe (el proxy lo sigue con `get-validation-status`,
como la app). Los listados lo muestran desde el primer momento.

**WebDAV completo** (`golang.org/x/net/webdav`):

| | |
|---|---|
| PROPFIND | Depth 0/1/infinity; tamaño, fechas, `getetag`, tipo, y la **cuota** (RFC 4331) |
| GET / HEAD | rangos exactos pedidos a O2 y reenviados en streaming; HEAD no descarga |
| PUT | ver arriba; `X-OC-Mtime` fija la fecha de modificación |
| MOVE | renombrar/mover en O2 (ficheros y carpetas), sin volver a subir |
| COPY | ficheros: copia dentro de O2 (sin descargar ni subir) |
| DELETE / MKCOL | borrar va a la papelera; las carpetas con más elementos de los que O2 deja borrar de una vez (`FOL-1036`) se vacían por partes |
| LOCK / UNLOCK | bloqueos reales (en memoria) |
| PROPPATCH | propiedades guardadas en memoria |
| Basura del sistema | `.DS_Store`, `Thumbs.db`, `desktop.ini`, `._*`… se aceptan y se descartan |

**Errores con sentido**: los fallos pasajeros de O2 (red, 5xx, cortafuegos) y
la sesión caducada se responden `503` + `Retry-After`, para que Hyper Backup
reintente en vez de dar la copia por rota; `413`/`415` cuando O2 rechaza el
fichero por tamaño o contenido.

**Conexión con O2**: TLS estándar de Go con HTTP/1.1 (anunciado por ALPN), conexiones
reutilizadas, y en Linux las conexiones muertas se detectan en 60 s
(`TCP_USER_TIMEOUT`). Una descarga que pasa 60 s sin recibir nada de O2 se corta
(el cliente reintenta) y las llamadas pequeñas al API tienen un límite de 2 min.
O2 está detrás de CloudFront, que rechaza (401 vacío) las conexiones TLS que no
anuncian protocolo.

**Sesión**: la *validation key* que rota O2 se adopta sola; cada
`keepalive_minutes` (5) hay una petición ligera para que O2 no cierre la sesión
por inactividad. Si aun así caduca, el panel lo marca y el log lo avisa.

**Tipo de subida**: por defecto **todo se sube como fichero genérico**, tal
cual. Con `upload_as_media: true` (o la casilla del panel) las fotos, vídeos y
audio se declaran como tales y O2 puede procesarlos (y rechazar los que no lo
sean de verdad).

**Paralelismo**: «Subidas / Descargas a la vez» en el panel (o `transfers:` en
el config). **0 = sin límite** (por defecto); con límite, el resto espera «en
cola».

## Panel

- Cuentas: usuario/contraseña WebDAV, estado de la sesión, renovar sesión,
  explorador de O2 (prueba las credenciales) con cuota y límites.
- **Transferencias** en curso: progreso, velocidad, fase (recibiendo, subiendo,
  reintento, validando, en cola…) y las últimas terminadas.
- **Caché de escritura**: qué ficheros recién subidos se sirven desde la copia
  local y cuándo caducan; los segundos se ajustan por cuenta.
- **Registro en vivo** (INFO y superior; el log completo con DEBUG va a
  `log_file`).
- **Ajustes**: paralelismo, keepalive y tipo de subida (genérico o multimedia).

## Línea de comandos

```
funambridge [--config config.yaml] [-v] [--log-file f] <subcomando>
  serve        sirve WebDAV + panel (por defecto)
  import-har   importa una sesión de un HAR
  probe        comprueba la sesión, límites y espacio de una cuenta
  dedupe       busca/repara copias duplicadas de versiones antiguas (--apply)
  health       healthcheck (¿responde el servidor?)
```

## Desarrollo

```bash
go vet ./... && go test ./...        # tests contra un O2 simulado (internal/fakeo2)
FUNAMBRIDGE_LIVE_CONFIG=config.yaml FUNAMBRIDGE_LIVE_ACCOUNT=prueba \
  go test -tags live -run Live -v ./internal/sapi/   # contra O2 de verdad (carpeta de pruebas)
```

| Paquete | Función |
|---|---|
| `cmd/funambridge` | CLI |
| `internal/config` | config YAML (compatible con 0.x) |
| `internal/sapi` | cliente de O2: sesión, transporte, endpoints, subidas |
| `internal/login`, `internal/har` | login por SMS, import de HAR |
| `internal/store` | rutas ⇄ O2, índice por carpeta, seguimiento de la validación |
| `internal/davfs` | sistema de ficheros para `x/net/webdav` |
| `internal/server` | servidor HTTP, WebDAV, panel, keepalive |
| `internal/transfers`, `internal/logx` | transferencias, log |
| `internal/repair` | `dedupe` |
| `internal/fakeo2` | O2 simulado para los tests |

## Proyectos similares

- [MovistarWebDAVProxy](https://github.com/uniextra/MovistarWebDAVProxy):
  WebDAV para Movistar Cloud y O2 Cloud.

## Aviso

Uso personal contra tu propia cuenta de O2 Cloud. `config.yaml` contiene tu
sesión activa: trátalo como una contraseña (está excluido de git).
