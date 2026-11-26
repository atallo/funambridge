"""
Admin web UI for funambridge.

Rendered by the *same* HTTP server as the S3 API (single port). The S3 handler
dispatches browser requests here; this module is just the HTML, with
no server of its own. Admin routes live under the ADMIN_PREFIX.
"""

import html
import time

from . import __version__

ADMIN_PREFIX = "/_admin"
O2_WEB = "https://cloud.o2online.es"      # la web de O2 Cloud (enlace en el panel)

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


def _human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{int(n)} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def cache_rows(cfg, cache_overview):
    """Flatten the write-cache overview into display rows (used by the panel and
    the /cache.json refresh endpoint)."""
    name_by_ak = {a.access_key: a.name for a in cfg.accounts}
    rows = []
    for ak, entries in (cache_overview or {}).items():
        acc = name_by_ak.get(ak, ak)
        for e in entries:
            rows.append({"account": acc, "path": e["path"],
                         "size": _human(e["size"]), "where": e["where"],
                         "expires": int(e["expires_in"])})
    rows.sort(key=lambda r: (r["account"], r["path"]))
    return rows


def _cache_tbody(rows):
    if not rows:
        return ("<tr><td colspan='5' class='muted' style='text-align:center;"
                "padding:18px'>La caché está vacía.</td></tr>")
    return "".join(
        f"<tr><td>{html.escape(r['account'])}</td>"
        f"<td class='mono small'>{html.escape(r['path'])}</td>"
        f"<td>{html.escape(r['size'])}</td><td>{html.escape(r['where'])}</td>"
        f"<td>{r['expires']} s</td></tr>" for r in rows)


def _explorer_card(cfg):
    """Card to browse an account's files live on the server (fresh calls, no
    cache), which doubles as a credentials check."""
    if not cfg.accounts:
        return ""
    opts = "".join(f'<option value="{html.escape(a.name)}">{html.escape(a.name)}'
                   f'</option>' for a in cfg.accounts)
    return f"""
 <div class="card" id="fb-ex">
  <h2>Explorador del servidor</h2>
  <p class="muted small" style="margin:-6px 0 12px">Lista los ficheros directamente
   desde O2, sin caché: si carga, las credenciales de la cuenta siguen siendo válidas.</p>
  <div class="explbar">
   <select id="fb-ex-acc">{opts}</select>
   <button type="button" class="pri" onclick="fbExLoad('')">Probar y listar</button>
   <span id="fb-ex-status"></span>
  </div>
  <div id="fb-ex-crumbs" class="crumbs"></div>
  <table class="mt" id="fb-ex-table" style="display:none">
   <thead><tr><th>Nombre</th><th>Tamaño</th><th>Modificado</th></tr></thead>
   <tbody id="fb-ex-rows"></tbody>
  </table>
  <p class="muted small" id="fb-ex-note"></p>
 </div>
 <script>
  function fbExEsc(s){{return String(s).replace(/[&<>"']/g,function(c){{
    return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c];}});}}
  function fbExHuman(n){{n=Number(n||0);var u=['B','KB','MB','GB','TB'],i=0;
    while(n>=1024&&i<u.length-1){{n/=1024;i++;}} return (i?n.toFixed(1):n)+' '+u[i];}}
  function fbExDate(v){{v=Number(v||0); if(!v) return ''; if(v<1e11) v*=1000;
    return new Date(v).toLocaleString();}}
  var fbExPath='';
  function fbExLoad(path){{
    var acc=document.getElementById('fb-ex-acc').value,
        st=document.getElementById('fb-ex-status');
    st.className='muted small'; st.textContent='Consultando al servidor…';
    fetch('{ADMIN_PREFIX}/browse.json?name='+encodeURIComponent(acc)+
          '&path='+encodeURIComponent(path))
      .then(function(r){{return r.json();}}).then(fbExRender)
      .catch(function(e){{st.className='tag bad exstat';
        st.textContent='✗ No se pudo contactar con el proxy: '+e;}});
  }}
  function fbExRender(d){{
    var st=document.getElementById('fb-ex-status'), tb=document.getElementById('fb-ex-rows'),
        tbl=document.getElementById('fb-ex-table'), note=document.getElementById('fb-ex-note'),
        cr=document.getElementById('fb-ex-crumbs');
    if(!d.ok){{
      var hint={{'session':' — la sesión ha caducado: usa «Renovar sesión» con un HAR fresco.',
                 'noauth':' — añade una sesión a la cuenta.',
                 'upstream':' — error de O2; reinténtalo en un rato.'}}[d.kind]||'';
      st.className='tag bad exstat'; st.textContent='✗ '+(d.error||'error')+hint;
      tbl.style.display='none'; note.textContent=''; return;
    }}
    fbExPath=d.path||'';
    var s='✓ Credenciales válidas · comprobado a las '+d.checked;
    if(d.storage&&d.storage.quota){{
      s+=' · usado '+fbExHuman(d.storage.used)+' de '+fbExHuman(d.storage.quota);}}
    st.className='tag ok exstat'; st.textContent=s;
    var parts=fbExPath?fbExPath.split('/'):[], h='<a href="#" data-p="">raíz</a>', acc='';
    for(var i=0;i<parts.length;i++){{acc+=(i?'/':'')+parts[i];
      h+=' / <a href="#" data-p="'+fbExEsc(acc)+'">'+fbExEsc(parts[i])+'</a>';}}
    cr.innerHTML=h;
    var rows='';
    if(fbExPath){{rows+='<tr class="exdir"><td colspan="3"><a href="#" data-p="'+
      fbExEsc(parts.slice(0,-1).join('/'))+'">⬑ ..</a></td></tr>';}}
    d.folders.forEach(function(f){{var p=(fbExPath?fbExPath+'/':'')+f;
      rows+='<tr class="exdir"><td colspan="3"><a href="#" data-p="'+fbExEsc(p)+'">📁 '+
        fbExEsc(f)+'</a></td></tr>';}});
    d.files.forEach(function(f){{rows+='<tr><td class="mono small">'+fbExEsc(f.name)+
      '</td><td>'+fbExHuman(f.size)+'</td><td class="small">'+fbExEsc(fbExDate(f.modified))+
      '</td></tr>';}});
    if(!rows) rows="<tr><td colspan='3' class='muted' style='text-align:center;padding:18px'>Carpeta vacía.</td></tr>";
    tb.innerHTML=rows; tbl.style.display='';
    note.textContent=d.folders.length+' carpetas · '+d.total_files+' ficheros'+
      (d.total_files>d.files.length?' (mostrando los '+d.files.length+' primeros)':'');
  }}
  document.getElementById('fb-ex').addEventListener('click',function(ev){{
    var a=ev.target.closest('a[data-p]'); if(!a) return;
    ev.preventDefault(); fbExLoad(a.getAttribute('data-p'));}});
  function fbExOpen(name){{document.getElementById('fb-ex-acc').value=name;
    document.getElementById('fb-ex').scrollIntoView({{behavior:'smooth'}}); fbExLoad('');}}
 </script>"""


def _cache_card(cfg, cache_overview):
    """Collapsible card listing the write-cache; refreshes in place (no reload,
    so it doesn't collapse) via /cache.json, with optional auto-refresh."""
    rows = cache_rows(cfg, cache_overview)
    return f"""
 <div class="card">
  <details id="fb-cache" ontoggle="if(this.open)fbCacheRefresh()">
   <summary class="link">Caché de escritura
     <span class="badge" id="fb-cache-count">{len(rows)}</span></summary>
   <div class="cachebar">
    <button type="button" class="ghost" onclick="fbCacheRefresh()">↻ Actualizar</button>
    <label class="chk"><input type="checkbox" id="fb-cache-auto"> auto (3 s)</label>
    <span class="muted small">actualizado: <span id="fb-cache-time">—</span></span>
   </div>
   <table class="mt">
    <thead><tr><th>Cuenta</th><th>Fichero</th><th>Tamaño</th><th>Dónde</th>
      <th>Caduca</th></tr></thead>
    <tbody id="fb-cache-rows">{_cache_tbody(rows)}</tbody>
   </table>
  </details>
 </div>
 <script>
  function fbEsc(s){{return String(s).replace(/[&<>"]/g,function(c){{
    return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c];}});}}
  function fbCacheRender(d){{
    var c=document.getElementById('fb-cache-count'); if(c) c.textContent=d.length;
    var tb=document.getElementById('fb-cache-rows'); if(!tb) return;
    if(!d.length){{tb.innerHTML="<tr><td colspan='5' class='muted' style='text-align:center;padding:18px'>La caché está vacía.</td></tr>";}}
    else{{tb.innerHTML=d.map(function(r){{return "<tr><td>"+fbEsc(r.account)+
      "</td><td class='mono small'>"+fbEsc(r.path)+"</td><td>"+fbEsc(r.size)+
      "</td><td>"+fbEsc(r.where)+"</td><td>"+r.expires+" s</td></tr>";}}).join("");}}
    var t=document.getElementById('fb-cache-time');
    if(t) t.textContent=new Date().toLocaleTimeString();
  }}
  function fbCacheRefresh(){{fetch('{ADMIN_PREFIX}/cache.json')
    .then(function(r){{return r.json();}}).then(fbCacheRender).catch(function(){{}});}}
  (function(){{var a=document.getElementById('fb-cache-auto'); if(!a) return; var t=null;
    a.addEventListener('change',function(){{
      if(a.checked){{fbCacheRefresh(); t=setInterval(fbCacheRefresh,3000);}}
      else{{clearInterval(t);}}}});}})();
 </script>"""


def _pending_card(pending):
    """Card asking for the SMS code of a login started from the panel."""
    if not pending:
        return ""
    tok = html.escape(pending["token"])
    return f"""
 <div class="card pend">
  <h2>Código del SMS</h2>
  <p style="margin:0 0 12px">O2 ha enviado un SMS al {html.escape(pending["masked"])} para
   iniciar sesión en la cuenta <b>{html.escape(pending["name"])}</b>. Escribe el código
   (caduca en unos minutos).</p>
  <form method="post" action="{ADMIN_PREFIX}/login-verify" class="explbar">
   <input type="hidden" name="token" value="{tok}">
   <input name="code" inputmode="numeric" autocomplete="one-time-code" placeholder="código"
          required autofocus style="width:130px;font-size:18px;letter-spacing:.15em">
   <button class="pri">Validar e iniciar sesión</button>
  </form>
  <form method="post" action="{ADMIN_PREFIX}/login-cancel" class="mt">
   <input type="hidden" name="token" value="{tok}">
   <button class="ghost">Cancelar</button>
  </form>
 </div>"""


def _alive_tag(st):
    """Small status line with the keepalive result of one account."""
    if not st:
        return ""
    hm = lambda t: time.strftime("%d/%m %H:%M", time.localtime(t))  # noqa: E731
    if st.get("fail"):
        return (f'<div class="mt"><span class="tag bad" title="O2 rechazó la '
                f'sesión el {hm(st["fail"])}">caducada · renueva la sesión'
                f'</span></div>')
    if st.get("ok"):
        return (f'<div class="muted small mt" title="El proxy hace una petición '
                f'cada pocos minutos para que O2 no cierre la sesión por '
                f'inactividad">viva · comprobada {hm(st["ok"])}</div>')
    return ""


def render_page(cfg, msg="", err="", endpoint=None, cache_overview=None,
                pending=None, alive=None):
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
        if a.session.is_set() and not a.device_id.startswith("web-"):
            # O2 only accepts uploads from the device the session belongs to
            auth += ('<div class="mt"><span class="tag bad" title="Esta sesión no es de '
                     'este dispositivo: O2 rechazará las subidas. Renueva la sesión '
                     '(SMS o HAR).">solo lectura · renueva la sesión</span></div>')
        if a.has_auth():
            auth += _alive_tag((alive or {}).get(a.access_key))
        phone_digits = html.escape(a.phone or "")
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
            <details class="mt"><summary class="link">Cambiar claves</summary>
             <div class="pop">
              <form method="post" action="{ADMIN_PREFIX}/keys">
                <input type="hidden" name="name" value="{nm}">
                <input type="hidden" name="mode" value="regen">
                <button>Regenerar aleatorias</button></form>
              <form method="post" action="{ADMIN_PREFIX}/keys">
                <span class="lbl">Fijar manualmente</span>
                <input type="hidden" name="name" value="{nm}">
                <input type="hidden" name="mode" value="set">
                <input name="access_key" placeholder="access_key">
                <input name="secret_key" placeholder="secret_key">
                <button class="pri">Fijar</button></form>
             </div>
            </details>
          </td>
          <td>{toggle(nm, "s3", a.s3_enabled)}</td>
          <td>{toggle(nm, "webdav", a.webdav_enabled)}</td>
          <td>
            <form method="post" action="{ADMIN_PREFIX}/cache" style="display:flex;gap:5px;align-items:center">
              <input type="hidden" name="name" value="{nm}">
              <input name="seconds" type="number" min="0" value="{a.cache_seconds}"
                     style="width:62px" title="caché de escritura en segundos (0 = off)">
              <span class="muted small">s</span>
              <button class="ghost" title="guardar">✓</button></form>
          </td>
          <td>{auth}</td>
          <td>
           <div class="acts">
            <button type="button" class="ghost" data-acc="{nm}"
                    onclick="fbExOpen(this.dataset.acc)"
                    title="probar las credenciales y ver los ficheros en O2">Explorar</button>
            <button type="button" class="ghost"
                    onclick="document.getElementById('rn{i}').showModal()">Renovar sesión</button>
            <form method="post" action="{ADMIN_PREFIX}/remove" onsubmit="return confirm('¿Eliminar la cuenta {nm}?')">
              <input type="hidden" name="name" value="{nm}">
              <button class="danger">Eliminar</button></form>
           </div>
           <dialog class="dlg" id="rn{i}">
            <header><b>Renovar sesión · {nm}</b>
             <form method="dialog"><button class="ghost" aria-label="cerrar">✕</button></form></header>
            <div class="dbody">
             <section class="hl">
              <h4>Con SMS <span class="tag ok">recomendado</span></h4>
              <p class="muted small">El proxy inicia sesión en O2 por sí mismo: te llega un
               SMS y escribes el código en la pantalla siguiente.</p>
              <form method="post" action="{ADMIN_PREFIX}/login-start" class="drow">
                <input type="hidden" name="name" value="{nm}">
                <input name="phone" value="{phone_digits}" placeholder="tu número de O2"
                       inputmode="tel" autocomplete="tel" required>
                <button class="pri">Enviar SMS</button></form>
             </section>
             <section>
              <h4>Con un HAR</h4>
              <p class="muted small">Inicia sesión en <a href="{O2_WEB}" target="_blank"
               rel="noopener">O2 Cloud ↗</a> con DevTools abierto (pestaña Red) y exporta el HAR completo.</p>
              <form method="post" action="{ADMIN_PREFIX}/import-har" enctype="multipart/form-data" class="drow">
                <input type="hidden" name="name" value="{nm}">
                <input type="file" name="har" accept=".har,application/json" required>
                <button>Subir HAR</button></form>
             </section>
             <details class="adv"><summary class="link">Avanzado: pegar las cookies</summary>
              <form method="post" action="{ADMIN_PREFIX}/set-session" class="dcol">
                <input type="hidden" name="name" value="{nm}">
                <input class="mono" name="jsessionid" placeholder="JSESSIONID" required>
                <input class="mono" name="validationkey" placeholder="validationKey" required>
                <input class="mono" name="deviceid" placeholder="x-deviceid (web-…, necesario para subir)"
                       title="DevTools → Red → cualquier petición /sapi → cabecera x-deviceid">
                <input class="mono" name="plc" placeholder="PLC (opcional)">
                <button>Guardar cookies</button></form>
             </details>
            </div>
           </dialog>
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
 .ver{{font-size:13px;font-weight:400;color:var(--muted)}}
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
 summary.link{{color:var(--pri);cursor:pointer;font-size:13px;list-style:none;
   display:inline-flex;align-items:center;gap:6px;user-select:none}}
 summary.link::-webkit-details-marker{{display:none}}
 summary.link::before{{content:"▸";font-size:10px;transition:transform .15s}}
 details[open]>summary.link::before{{transform:rotate(90deg)}}
 summary.link:hover{{text-decoration:underline}}
 /* styled drop-down panel for the in-row forms (renovar sesión / claves) */
 .pop{{margin-top:8px;padding:12px 13px;background:#f8fafc;border:1px solid var(--bd);
   border-radius:10px;display:flex;flex-direction:column;gap:12px;min-width:235px}}
 .pop form{{display:flex;flex-direction:column;gap:6px;margin:0}}
 .pop input{{width:100%}}
 .pop .lbl{{font-size:10px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;
   color:var(--muted)}}
 .badge{{background:#eef1f4;color:#374151;border-radius:999px;padding:1px 9px;
   font-size:12px;font-weight:600}}
 .cachebar{{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-top:12px}}
 .chk{{display:inline-flex;align-items:center;gap:5px;color:var(--muted);font-size:12px;
   cursor:pointer}}
 /* explorador del servidor */
 .explbar{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
 /* login por SMS: tarjeta del código */
 .card.pend{{border:2px solid var(--pri);background:#f5f8ff}}
 select{{padding:7px 9px;border:1px solid var(--bd);border-radius:8px;background:#fff}}
 .exstat{{border-radius:8px;padding:5px 10px;line-height:1.4;display:inline-block}}
 .crumbs{{margin-top:12px;font-size:13px}}
 .crumbs a{{color:var(--pri);text-decoration:none}} .crumbs a:hover{{text-decoration:underline}}
 tr.exdir a{{color:var(--fg);text-decoration:none;font-weight:500}}
 tr.exdir a:hover{{color:var(--pri)}}
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
 /* cabecera con enlace a la web de O2 */
 .top{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}}
 .o2link{{display:inline-block;padding:8px 14px;border:1px solid var(--bd);border-radius:9px;
   background:#fff;color:var(--pri);text-decoration:none;font-weight:600;font-size:14px}}
 .o2link:hover{{background:#f1f5ff}}
 /* acciones de cada cuenta + ventana "Renovar sesión" */
 .acts{{display:flex;flex-wrap:wrap;gap:6px;align-items:center}}
 .acts form{{margin:0}}
 dialog.dlg{{border:0;border-radius:14px;padding:0;width:min(560px,92vw);color:var(--fg);
   box-shadow:0 24px 64px rgba(16,24,40,.28)}}
 dialog.dlg::backdrop{{background:rgba(15,23,42,.45)}}
 .dlg header{{display:flex;justify-content:space-between;align-items:center;
   padding:14px 18px;border-bottom:1px solid var(--bd)}}
 .dlg header form{{margin:0}}
 .dbody{{padding:16px 18px 18px;display:flex;flex-direction:column;gap:14px}}
 .dbody section{{border:1px solid var(--bd);border-radius:10px;padding:12px 14px}}
 .dbody section.hl{{border-color:var(--pri);background:#f5f8ff}}
 .dbody h4{{margin:0 0 4px;font-size:14px;display:flex;align-items:center;gap:8px}}
 .dbody p{{margin:0 0 10px}}
 .drow{{display:flex;gap:8px;flex-wrap:wrap;margin:0}} .drow input{{flex:1;min-width:180px}}
 .dcol{{display:flex;flex-direction:column;gap:8px;margin:10px 0 0}}
</style><script>{_COPY_JS}</script></head><body>
<div class="wrap">
 <div class="top">
  <div><h1>funambridge <span class="ver">v{html.escape(__version__)}</span></h1>
   <p class="sub">Acceso a O2 Cloud por <b>S3</b> y <b>WebDAV</b></p></div>
  <a class="o2link" href="{O2_WEB}" target="_blank" rel="noopener"
     title="{O2_WEB}">Abrir O2 Cloud ↗</a>
 </div>
 {banner}
 {_pending_card(pending)}

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
    <div class="kv"><b>Direccionamiento</b> <code>path-style</code></div>
    {("<div class='kv'><b>Ficheros de la raíz</b> en el bucket virtual <code>"
      + html.escape(cfg.root_bucket) + "</code></div>") if cfg.root_bucket else ""}
   </div>
  </div>
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

    <h3>Opción A — Iniciar sesión con tu número (recomendado)</h3>
    <p class="muted small">El proxy inicia sesión en O2 por sí mismo: te llega un SMS y
     escribes el código aquí. Sin HAR ni cookies.</p>
    <form method="post" action="{ADMIN_PREFIX}/login-start" class="inline">
     <input name="name" placeholder="nombre de la cuenta" required>
     <input name="phone" placeholder="tu número de O2" inputmode="tel" autocomplete="tel" required>
     <button class="pri">Enviar SMS</button>
    </form>

    <h3>Opción B — Subir un HAR completo</h3>
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

    <h3>Opción C — Pegar las cookies</h3>
    <p class="muted small">DevTools → Application/Almacenamiento → Cookies → cloud.o2online.es</p>
    <form method="post" action="{ADMIN_PREFIX}/set-session">
     <div class="kv"><input name="name" placeholder="nombre"></div>
     <div class="kv"><input class="mono" name="jsessionid" placeholder="JSESSIONID" size="46" required></div>
     <div class="kv"><input class="mono" name="validationkey" placeholder="validationKey" size="46" required></div>
     <div class="kv"><input class="mono" name="plc" placeholder="PLC (opcional)" size="46"></div>
     <div class="kv"><input class="mono" name="deviceid" placeholder="x-deviceid (web-…, necesario para subir)" size="46"
       title="DevTools → Red → cualquier petición /sapi → cabecera x-deviceid"></div>
     <button class="pri">Guardar cuenta</button>
    </form>

    <p class="note">El HAR y las cookies contienen tu sesión activa: trátalos como una contraseña. Todo se procesa en el proxy.</p>
   </div>
  </details>
 </div>
 {_explorer_card(cfg)}
 {_cache_card(cfg, cache_overview)}
</div>
</body></html>"""
