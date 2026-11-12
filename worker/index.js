// Sirve el tablero del IPCEER desde el release "sepa-archivo" de GitHub (tablero.html),
// que sube la PC al terminar cada corrida. Caché de 10 minutos en el borde.
// Si el worker tiene el secreto GITHUB_TOKEN, lo baja por la API (funciona aunque el repo sea privado).
const REPO = "tadeobessega/ipceer", TAG = "sepa-archivo", ARCHIVO = "tablero.html";
const ORIGEN = `https://github.com/${REPO}/releases/download/${TAG}/${ARCHIVO}`;

async function bajarTablero(env) {
  if (!env.GITHUB_TOKEN) return fetch(ORIGEN, { redirect: "follow", cf: { cacheTtl: 0 } });
  const h = { authorization: `Bearer ${env.GITHUB_TOKEN}`, "user-agent": "ipceer-worker", "x-github-api-version": "2022-11-28" };
  const rel = await fetch(`https://api.github.com/repos/${REPO}/releases/tags/${TAG}`, { headers: { ...h, accept: "application/vnd.github+json" } });
  if (!rel.ok) return rel;
  const a = ((await rel.json()).assets || []).find((x) => x.name === ARCHIVO);
  if (!a) return new Response(null, { status: 404 });
  const d = await fetch(`https://api.github.com/repos/${REPO}/releases/assets/${a.id}`, { headers: { ...h, accept: "application/octet-stream" }, redirect: "manual" });
  const loc = d.headers.get("location");
  return loc ? fetch(loc) : d;
}
const CACHE_SEG = 600;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/favicon.ico") return new Response(null, { status: 204 });
    if (url.pathname !== "/" && url.pathname !== "/index.html") {
      return Response.redirect(url.origin + "/", 302);
    }
    const cache = caches.default;
    const clave = new Request(url.origin + "/__tablero");
    const fresco = url.searchParams.has("actualizar");
    if (!fresco) {
      const enCache = await cache.match(clave);
      if (enCache) return enCache;
    }
    const r = await bajarTablero(env);
    if (!r.ok) return pendiente();
    const resp = new Response(r.body, {
      headers: {
        "content-type": "text/html; charset=utf-8",
        "cache-control": `public, max-age=${CACHE_SEG}`,
        "x-content-type-options": "nosniff",
      },
    });
    ctx.waitUntil(cache.put(clave, resp.clone()));
    return resp;
  },
};

function pendiente() {
  return new Response(`<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ipceer</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap" rel="stylesheet">
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#f6f7f9;color:#171a24;font-family:Inter,sans-serif}
main{max-width:520px;padding:32px;background:#fff}h1{font-size:24px;margin:0 0 12px}p{color:#5d6069;line-height:1.5;margin:0}</style></head>
<body><main><h1>ipceer</h1><p>El tablero se publica después de la próxima corrida de la tarea diaria. Volvé a entrar en un rato.</p></main></body></html>`,
    { status: 503, headers: { "content-type": "text/html; charset=utf-8", "retry-after": "3600" } });
}
