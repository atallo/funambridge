# funambridge 0.x (Python) — versión antigua

Esta es la versión en Python (hasta la 0.2.x). La versión actual está escrita en
Go, en la raíz del repositorio.

Se mantiene como estaba en la 0.2.1: WebDAV, login por SMS/HAR/cookies,
keepalive, descargas en streaming, comando `dedupe`. Imagen Docker:
`ghcr.io/atallo/funambridge:0.2` (y `latest` mientras la 1.x esté en beta).

```bash
cd legacy/python
pip install -r requirements.txt        # solo PyYAML
python -m funambridge serve            # http://0.0.0.0:9000 (panel en /_admin/)
```

Los tags `v0.*` se siguen publicando desde este directorio.
