"""
Admin web UI for funambridge.

Rendered by the *same* HTTP server as the S3 API (single port). The S3 handler
dispatches browser requests here; this module is just the snippet + HTML, with
no server of its own. Admin routes live under the ADMIN_PREFIX.
"""

import html

ADMIN_PREFIX = "/_admin"

# JS snippet pasted into the O2 web console (cloud.o2online.es origin).
#
# Flow (COOP/popup-proof, manual return-URL paste):
#   1. action=start (platform=android -> redirect lands on the passive
#      clientoauth.html, which never consumes the code or closes the window);
#   2. open the authorize URL in a NEW TAB; the user completes the SMS there;
#   3. the user copies the clientoauth.html URL (code+state stay in it) and
#      pastes it back into the panel;
#   4. action=validate -> OAuth tokens -> base64 blob to paste into the proxy.
CONSOLE_SNIPPET = r"""
(() => {
  const VERSION = 'v5';
  const ORIGIN = location.origin;
  const log = (...a) => console.log('[funambridge]', ...a);
  log('snippet', VERSION, 'cargado');
  if (!/o2online\.es$/.test(location.hostname)) {
    alert("Abre la consola en https://cloud.o2online.es y pega el snippet ahí.");
    return;
  }
  const old = document.getElementById('o2s3-box'); if (old) old.remove();
  const box = document.createElement('div');
  box.id = 'o2s3-box';
  box.style.cssText = 'position:fixed;z-index:2147483647;right:16px;bottom:16px;width:430px;'
    + 'background:#111;color:#eee;font:13px/1.45 sans-serif;padding:14px;border-radius:10px;'
    + 'box-shadow:0 6px 30px rgba(0,0,0,.5)';
  box.innerHTML =
      '<b>funambridge <span style="color:#6cf">' + VERSION + '</span></b>'
    + '<div id="o2s3-msg" style="margin:8px 0;color:#bbb">Pulsa «Conectar cuenta».</div>'
    + '<button id="o2s3-go" style="padding:8px 12px;border:0;border-radius:6px;cursor:pointer">Conectar cuenta</button>'
    + '<div id="o2s3-2" style="display:none;margin-top:10px">'
    +   '<div id="o2s3-link" style="margin-bottom:6px"></div>'
    +   '<div style="color:#bbb;margin-bottom:4px">Cuando acabes el SMS verás una página con un círculo girando. '
    +     'Copia su URL (barra de direcciones) y pégala aquí:</div>'
    +   '<input id="o2s3-url" placeholder=".../clientoauth.html?code=..." '
    +     'style="width:100%;box-sizing:border-box;padding:6px;background:#000;color:#eee;border:1px solid #333;border-radius:6px">'
    +   '<button id="o2s3-val" style="margin-top:6px;padding:8px 12px;border:0;border-radius:6px;cursor:pointer">Validar y generar código</button>'
    + '</div>'
    + '<div id="o2s3-3" style="display:none;margin-top:10px">'
    +   '<textarea id="o2s3-out" readonly style="width:100%;box-sizing:border-box;height:88px;'
    +     'background:#000;color:#6f6;border:1px solid #333;border-radius:6px"></textarea>'
    +   '<button id="o2s3-copy" style="margin-top:6px;padding:8px 12px;border:0;border-radius:6px;cursor:pointer">Copiar código</button>'
    + '</div>';
  document.body.appendChild(box);
  const $ = (id) => document.getElementById(id);
  const msg = (t) => { $('o2s3-msg').textContent = t; log(t); };
  const sapi = async (path, opts) => {
    const r = await fetch(ORIGIN + path, Object.assign({credentials:'include',
      headers:{'Accept':'application/json','Content-Type':'application/json'}}, opts));
    const t = await r.text(); let j; try { j = JSON.parse(t); } catch(e) { j = {_raw:t}; }
    log('SAPI', path, '->', r.status, j); return j;
  };

  $('o2s3-go').onclick = async () => {
    try {
      let phone = prompt('Tu número O2 con prefijo de país (ej. +34668842867):', '+34');
      if (!phone) return;
      phone = phone.replace(/[\s-]/g,'');
      if (phone.startsWith('00')) phone = '+' + phone.slice(2);
      if (!phone.startsWith('+')) phone = (phone.length === 9 ? '+34' + phone : '+' + phone);
      log('msisdn normalizado =', phone);
      window.o2s3phone = phone;
      msg('Solicitando login…');
      const start = await sapi('/sapi/login/mobileconnect?action=start&platform=android&msisdn='
        + encodeURIComponent(phone), {method:'POST', body:'{}'});
      const url = start && start.data && start.data.authorizationurl;
      if (!url) { msg('Error en start: ' + JSON.stringify(start)); return; }
      log('authorizationurl =', url);
      const tab = window.open(url, '_blank');
      const a = document.createElement('a'); a.href = url; a.target = '_blank';
      a.textContent = 'Abrir login de O2 (si no se abrió solo)'; a.style.color = '#6cf';
      const link = $('o2s3-link'); link.textContent = ''; link.appendChild(a);
      $('o2s3-2').style.display = 'block';
      msg(tab ? 'Se abrió una pestaña. Completa el SMS y vuelve aquí.'
              : 'Tu navegador bloqueó la pestaña: pulsa el enlace de arriba.');
    } catch(e) { msg('Error: ' + e); }
  };

  $('o2s3-val').onclick = async () => {
    try {
      const ret = $('o2s3-url').value.trim();
      if (!ret) { msg('Pega la URL de la página del círculo girando.'); return; }
      let code, state, errd;
      try { const u = new URL(ret); const q = u.searchParams,
        h = new URLSearchParams((u.hash||'').replace(/^#/,''));
        code = q.get('code') || h.get('code'); state = q.get('state') || h.get('state');
        errd = q.get('error_description') || q.get('error') || h.get('error_description');
      } catch(e) {}
      if (errd) { msg('La URL trae un error de O2: ' + errd + '. Revisa el número (con +prefijo) y reintenta «Conectar cuenta».'); return; }
      if (!code) { msg('No encuentro «code» en esa URL. Copia la URL completa de la página del spinner.'); return; }
      msg('Validando y obteniendo tokens…');
      const val = await sapi('/sapi/credential/mobileconnect?action=validate&platform=android',
        {method:'POST', body: JSON.stringify({data:{code, state}})});
      const d = (val && val.data) || val;
      if (!d || !(d.access_token || d.refresh_token)) { msg('Error en validate: ' + JSON.stringify(val)); return; }
      const blob = {name: window.o2s3phone, msisdn: d.msisdn || window.o2s3phone,
        access_token: d.access_token, refresh_token: d.refresh_token,
        expires_in: d.expires_in, platform: 'androidphone'};
      const b64 = btoa(unescape(encodeURIComponent(JSON.stringify(blob))));
      $('o2s3-3').style.display = 'block';
      const out = $('o2s3-out'); out.value = b64; out.focus(); out.select();
      msg('¡Listo! Pulsa «Copiar código» y pégalo en la página del proxy.');
    } catch(e) { msg('Error: ' + e); }
  };

  document.body.addEventListener('click', (ev) => {
    if (!ev.target || ev.target.id !== 'o2s3-copy') return;
    const out = $('o2s3-out'); if (!out.value) return; out.select();
    (navigator.clipboard ? navigator.clipboard.writeText(out.value) : Promise.reject())
      .then(() => msg('Copiado al portapapeles.'))
      .catch(() => { document.execCommand('copy'); msg('Copiado.'); });
  });
})();
"""

# Small helper used by the admin page's "Copiar" buttons (works on http/LAN too,
# where navigator.clipboard is unavailable: falls back to execCommand).
_COPY_JS = ("function o2copy(id){var t=document.getElementById(id);"
            "t.style.display='block';t.focus();t.select();"
            "try{navigator.clipboard.writeText(t.value);}catch(e){try{document.execCommand('copy');}catch(_){}}"
            "}")


def _split_endpoint(s3ep):
    scheme, _, netloc = s3ep.partition("://")
    if ":" in netloc:
        host, port = netloc.rsplit(":", 1)
    else:
        host, port = netloc, ("443" if scheme == "https" else "80")
    return host, port, ("Sí" if scheme == "https" else "No")


def render_page(cfg, msg="", err="", endpoint=None):
    msg = html.escape(msg or "")
    err = html.escape(err or "")
    s3ep = endpoint or f"http://{cfg.s3_host}:{cfg.s3_port}"
    host, port, tls = _split_endpoint(s3ep)

    def toggle(name, what, on):
        cls = "pill on" if on else "pill off"
        label = ("S3" if what == "s3" else "WebDAV")
        return (f'<form method="post" action="{ADMIN_PREFIX}/toggle" style="display:inline">'
                f'<input type="hidden" name="name" value="{name}">'
                f'<input type="hidden" name="what" value="{what}">'
                f'<input type="hidden" name="value" value="{0 if on else 1}">'
                f'<button class="{cls}" title="clic para cambiar">'
                f'{"● " if on else "○ "}{label}</button></form>')

    rows = ""
    for i, a in enumerate(cfg.accounts):
        nm = html.escape(a.name)
        ak, sk = html.escape(a.access_key), html.escape(a.secret_key)
        auth = ('<span class="tag ok">sesión</span>' if a.session.is_set()
                else '<span class="tag ok">tokens</span>' if a.oauth.is_set()
                else '<span class="tag bad">sin auth</span>')
        rows += f"""
        <tr>
          <td><div class="acc">{nm}</div><div class="muted small">{html.escape(a.phone)}</div></td>
          <td>
            <div class="keyrow"><span class="klbl">AK</span>
              <input id="ak{i}" class="mono" readonly value="{ak}">
              <button type="button" class="ghost" onclick="o2copy('ak{i}')">Copiar</button></div>
            <div class="keyrow"><span class="klbl">SK</span>
              <input id="sk{i}" class="mono" readonly value="{sk}">
              <button type="button" class="ghost" onclick="o2copy('sk{i}')">Copiar</button></div>
            <details class="mt"><summary class="link">cambiar claves</summary>
              <form method="post" action="{ADMIN_PREFIX}/keys" class="mt">
                <input type="hidden" name="name" value="{nm}">
                <input type="hidden" name="mode" value="regen">
                <button>Regenerar aleatorias</button></form>
              <form method="post" action="{ADMIN_PREFIX}/keys" class="mt inline">
                <input type="hidden" name="name" value="{nm}">
                <input type="hidden" name="mode" value="set">
                <input name="access_key" placeholder="access_key">
                <input name="secret_key" placeholder="secret_key">
                <button>Fijar</button></form>
            </details>
          </td>
          <td>{toggle(nm, "s3", a.s3_enabled)}</td>
          <td>{toggle(nm, "webdav", a.webdav_enabled)}</td>
          <td>
            <form method="post" action="{ADMIN_PREFIX}/cache" style="display:inline">
              <input type="hidden" name="name" value="{nm}">
              <input name="seconds" type="number" min="0" value="{a.cache_seconds}"
                     style="width:58px" title="caché de escritura en segundos (0 = off)">
              <button class="ghost">s</button></form>
          </td>
          <td>{auth}</td>
          <td>
            <details><summary class="link">Renovar sesión</summary>
              <form method="post" action="{ADMIN_PREFIX}/import-har" enctype="multipart/form-data" class="mt">
                <input type="hidden" name="name" value="{nm}">
                <input type="file" name="har" accept=".har,application/json" required>
                <button>Renovar con HAR</button></form>
              <form method="post" action="{ADMIN_PREFIX}/set-session" class="mt">
                <input type="hidden" name="name" value="{nm}">
                <input name="jsessionid" placeholder="JSESSIONID" required>
                <input name="validationkey" placeholder="validationKey" required>
                <input name="plc" placeholder="PLC">
                <button>Renovar con cookies</button></form>
            </details>
            <form method="post" action="{ADMIN_PREFIX}/remove" onsubmit="return confirm('¿Eliminar la cuenta {nm}?')" class="mt">
              <input type="hidden" name="name" value="{nm}">
              <button class="danger">Eliminar</button></form>
          </td>
        </tr>"""
    if not rows:
        rows = ('<tr><td colspan="7" class="muted" style="text-align:center;padding:24px">'
                'No hay cuentas todavía. Pulsa «Añadir cuenta nueva».</td></tr>')

    banner = ""
    if msg:
        banner += f'<div class="banner ok-b">{msg}</div>'
    if err:
        banner += f'<div class="banner err-b">{err}</div>'

    snippet = html.escape(CONSOLE_SNIPPET.strip())
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>funambridge</title>
<style>
 :root{{--bg:#f5f6f8;--card:#fff;--bd:#e4e7eb;--fg:#1f2933;--muted:#6b7280;
        --pri:#2563eb;--ok:#16a34a;--bad:#dc2626;--off:#9aa3af}}
 *{{box-sizing:border-box}}
 body{{font:15px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;
       background:var(--bg);color:var(--fg)}}
 .wrap{{max-width:1040px;margin:0 auto;padding:28px 18px 60px}}
 h1{{font-size:22px;margin:0}} .sub{{color:var(--muted);margin:2px 0 18px}}
 .card{{background:var(--card);border:1px solid var(--bd);border-radius:14px;
        padding:18px 20px;margin:16px 0;box-shadow:0 1px 3px rgba(16,24,40,.05)}}
 .card>h2{{font-size:12px;letter-spacing:.06em;text-transform:uppercase;
           color:var(--muted);margin:0 0 14px}}
 table{{width:100%;border-collapse:collapse}}
 th{{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);
     text-align:left;padding:6px 10px;border-bottom:2px solid var(--bd)}}
 td{{padding:12px 10px;border-bottom:1px solid var(--bd);vertical-align:top}}
 tr:last-child td{{border-bottom:0}}
 .acc{{font-weight:600}} .muted{{color:var(--muted)}} .small{{font-size:12px}}
 .mono{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}}
 input,button,select{{font:inherit}}
 input{{padding:7px 9px;border:1px solid var(--bd);border-radius:8px;background:#fff}}
 input[readonly]{{background:#f8fafc;color:#374151}}
 button{{padding:7px 12px;border:1px solid var(--bd);border-radius:8px;background:#fff;
         cursor:pointer}} button:hover{{background:#f1f3f5}}
 .ghost{{padding:5px 9px;font-size:13px;color:#374151}}
 .danger{{color:var(--bad);border-color:#f3c2c2}} .danger:hover{{background:#fdecec}}
 .pri{{background:var(--pri);color:#fff;border-color:var(--pri);font-weight:600}}
 .pri:hover{{background:#1d4ed8}}
 .pill{{border-radius:999px;padding:6px 13px;font-weight:600;font-size:13px}}
 .pill.on{{background:#e7f6ec;color:var(--ok);border-color:#bfe6cc}}
 .pill.off{{background:#f1f3f5;color:var(--off)}}
 .tag{{font-size:12px;padding:3px 9px;border-radius:999px}}
 .tag.ok{{background:#e7f6ec;color:var(--ok)}} .tag.bad{{background:#fdecec;color:var(--bad)}}
 .keyrow{{display:flex;gap:6px;align-items:center;margin:3px 0}}
 .keyrow input{{flex:1;min-width:90px;font-size:13px}}
 .klbl{{font-size:11px;color:var(--muted);width:20px}}
 .mt{{margin-top:8px}} .inline input{{margin-right:6px}}
 summary.link{{color:var(--pri);cursor:pointer;font-size:13px;list-style:none}}
 summary.link::-webkit-details-marker{{display:none}}
 .grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
 @media(max-width:680px){{.grid{{grid-template-columns:1fr}}}}
 .kv{{margin:4px 0}} .kv b{{display:inline-block;min-width:90px;color:var(--muted);font-weight:500}}
 code{{background:#eef1f4;padding:2px 6px;border-radius:6px;font-size:13px}}
 .banner{{padding:11px 14px;border-radius:10px;margin:14px 0}}
 .ok-b{{background:#ecfdf5;border:1px solid #a7f3d0;color:#065f46}}
 .err-b{{background:#fef2f2;border:1px solid #fecaca;color:#991b1b}}
 details.add>summary{{list-style:none}} details.add>summary::-webkit-details-marker{{display:none}}
 .addhead{{display:inline-block;background:var(--pri);color:#fff;border-radius:9px;
           padding:9px 16px;font-weight:600;cursor:pointer}}
 .addbody{{margin-top:14px}} .addbody h3{{font-size:14px;margin:18px 0 6px}}
 .addbody ol{{padding-left:20px;color:#374151}} textarea{{width:100%;font-family:ui-monospace,monospace;
   border:1px solid var(--bd);border-radius:8px;padding:8px}}
 .note{{color:var(--bad);font-size:13px}}
</style><script>{_COPY_JS}</script></head><body>
<div class="wrap">
 <h1>funambridge</h1>
 <p class="sub">Acceso a O2 Cloud por <b>S3</b> y <b>WebDAV</b></p>
 {banner}

 <div class="card">
  <h2>Conexión &nbsp;·&nbsp; <span id="cx-ep" class="muted" style="text-transform:none;letter-spacing:0">{html.escape(s3ep)}</span></h2>
  <div class="grid">
   <div>
    <div class="kv"><b>WebDAV</b></div>
    <div class="kv"><b>URL</b> <code id="cx-url">{html.escape(s3ep)}</code></div>
    <div class="kv"><b>Usuario</b> el <code>access_key</code> de la cuenta</div>
    <div class="kv"><b>Contraseña</b> el <code>secret_key</code></div>
   </div>
   <div>
    <div class="kv"><b>S3</b></div>
    <div class="kv"><b>Host</b> <code id="cx-host">{html.escape(host)}</code> <span class="muted small">(solo el host, sin http:// ni puerto)</span></div>
    <div class="kv"><b>Puerto</b> <code id="cx-port">{html.escape(port)}</code></div>
    <div class="kv"><b>TLS / cifrado</b> <span id="cx-tls">{tls}</span></div>
    <div class="kv"><b>Direccionamiento</b> <code>path-style</code> · firma verificada
      <span class="muted small">(activa «path-style» en el cliente; con IP, virtual-host no funciona)</span></div>
    {("<div class='kv'><b>Ficheros de la raíz</b> en el bucket virtual <code>"
      + html.escape(cfg.root_bucket) + "</code></div>") if cfg.root_bucket else ""}
   </div>
  </div>
  <p class="muted small" style="margin:12px 0 0">El panel se deshabilita con <code>admin: {{enabled: false}}</code> en config.yaml.</p>
 </div>
 <script>
  (function(){{
    var https = location.protocol === 'https:';
    var port = location.port || (https ? '443' : '80');
    var set = function(id,v){{var e=document.getElementById(id); if(e) e.textContent=v;}};
    set('cx-ep', location.origin);
    set('cx-url', location.origin);
    set('cx-host', location.hostname);
    set('cx-port', port);
    set('cx-tls', https ? 'Sí (HTTPS)' : 'No (HTTP)');
  }})();
 </script>

 <div class="card">
  <h2>Cuentas</h2>
  <table>
   <thead><tr><th>Cuenta</th><th>Claves</th><th>S3</th><th>WebDAV</th><th>Caché</th><th>Auth</th><th>Acciones</th></tr></thead>
   <tbody>{rows}</tbody>
  </table>
  <details class="add mt">
   <summary><span class="addhead">＋ Añadir cuenta nueva</span></summary>
   <div class="addbody">
    <p class="muted">Para <b>actualizar</b> la sesión de una cuenta existente sin perder sus
    claves, usa «Renovar sesión» en su fila.</p>

    <h3>Opción A — Subir un HAR completo (recomendado)</h3>
    <ol>
     <li>F12 → pestaña <b>Network/Red</b>; activa la exportación de HAR <b>completo</b> (con datos sensibles, no «sanitized»).</li>
     <li>Inicia sesión en <a href="https://cloud.o2online.es/login" target="_blank">cloud.o2online.es</a> (teléfono + SMS).</li>
     <li>Guarda todo como HAR y súbelo:</li>
    </ol>
    <form method="post" action="{ADMIN_PREFIX}/import-har" enctype="multipart/form-data" class="inline">
     <input name="name" placeholder="nombre (opcional)">
     <input type="file" name="har" accept=".har,application/json" required>
     <button class="pri">Importar HAR</button>
    </form>

    <h3>Opción B — Pegar las cookies</h3>
    <p class="muted small">DevTools → Application/Almacenamiento → Cookies → cloud.o2online.es</p>
    <form method="post" action="{ADMIN_PREFIX}/set-session">
     <div class="kv"><input name="name" placeholder="nombre"></div>
     <div class="kv"><input class="mono" name="jsessionid" placeholder="JSESSIONID" size="46" required></div>
     <div class="kv"><input class="mono" name="validationkey" placeholder="validationKey" size="46" required></div>
     <div class="kv"><input class="mono" name="plc" placeholder="PLC (recomendado)" size="46"></div>
     <button class="pri">Guardar cuenta</button>
    </form>

    <h3>Opción C — Snippet de consola (desde el móvil)</h3>
    <p class="muted small">En escritorio suele fallar (<code>Incorrect phone number</code>); útil desde un navegador en el móvil.</p>
    <button type="button" onclick="o2copy('o2s3-snippet')">Copiar snippet</button>
    <textarea id="o2s3-snippet" rows="4" readonly onclick="this.select()" class="mt">{snippet}</textarea>
    <form method="post" action="{ADMIN_PREFIX}/add" class="mt">
     <div class="kv"><input name="name" placeholder="nombre (opcional)"></div>
     <textarea name="blob" rows="3" placeholder="pega aquí el base64 del snippet" required></textarea>
     <button class="pri mt">Guardar cuenta</button>
    </form>

    <p class="note">El HAR y las cookies contienen tu sesión activa: trátalos como una contraseña. Todo se procesa en el proxy.</p>
   </div>
  </details>
 </div>
</div>
</body></html>"""
