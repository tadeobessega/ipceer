"""Sonda chica: qué responde cada punto público del catálogo de unas pocas tiendas VTEX, de a un
pedido, con pausa. Sirve para saber si el límite (HTTP 429) es de un punto en particular o de todo."""
import json
import time

import requests

UA = "Mozilla/5.0 (compatible; ipceer-bot/1.0; +https://github.com/tadeobessega/ipceer)"
H = {"User-Agent": UA, "Accept": "application/json"}
HOSTS = ["www.carrefour.com.ar", "www.farmacity.com", "www.easy.com.ar", "www.jumbo.com.ar",
         "diaonline.supermercadosdia.com.ar", "www.farmaplus.com.ar", "www.topper.com.ar", "www.masonline.com.ar"]
RUTAS = {"buscar": "/api/catalog_system/pub/products/search?_from=0&_to=2",
         "arbol1": "/api/catalog_system/pub/category/tree/1",
         "inteligente": "/api/io/_v/api/intelligent-search/product_search/?page=1&count=3",
         "arbol3": "/api/catalog_system/pub/category/tree/3"}
out = {}
for h in HOSTS:
    out[h] = {}
    for k, ruta in RUTAS.items():
        time.sleep(3)
        try:
            r = requests.get(f"https://{h}{ruta}", headers=H, timeout=30)
            out[h][k] = {"status": r.status_code, "retry_after": r.headers.get("Retry-After"),
                         "server": r.headers.get("server"), "tipo": r.headers.get("content-type"),
                         "cuerpo": r.text[:160]}
        except Exception as e:
            out[h][k] = {"error": str(e)[:120]}
        print(h, k, out[h][k].get("status"), out[h][k].get("retry_after"))
json.dump(out, open("prueba_web.json", "w"), ensure_ascii=False, indent=1)
