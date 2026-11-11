"""Sonda: averigua qué tiendas en línea exponen su catálogo público (VTEX o Shopify), qué dice su
robots.txt y cómo es su árbol de categorías. Se corre en GitHub Actions; deja sonda_web.json."""
import json
import sys
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor

import requests

UA = "Mozilla/5.0 (compatible; ipceer-bot/1.0; +https://github.com/tadeobessega/ipceer)"
H = {"User-Agent": UA, "Accept": "application/json,text/html;q=0.8"}


def robots(host):
    try:
        r = requests.get(f"https://{host}/robots.txt", headers=H, timeout=20)
        if r.status_code != 200:
            return {"status": r.status_code}
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(r.text.splitlines())
        return {"status": 200, "api": rp.can_fetch("ipceer-bot", f"https://{host}/api/catalog_system/pub/products/search"),
                "products_json": rp.can_fetch("ipceer-bot", f"https://{host}/products.json"),
                "texto": r.text[:1500]}
    except Exception as e:
        return {"error": str(e)[:150]}


def vtex(host):
    out = {}
    try:
        r = requests.get(f"https://{host}/api/catalog_system/pub/category/tree/3", headers=H, timeout=30)
        out["tree_status"] = r.status_code
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
            def poda(n):
                return {"id": n.get("id"), "n": n.get("name"), "h": [poda(c) for c in n.get("children") or []]}
            out["tree"] = [poda(n) for n in r.json()]
            r2 = requests.get(f"https://{host}/api/catalog_system/pub/products/search", headers=H, timeout=30,
                              params={"_from": 0, "_to": 2})
            out["search_status"] = r2.status_code
            out["resources"] = r2.headers.get("resources")
            if r2.status_code in (200, 206):
                j = r2.json()
                out["ejemplo"] = [{k: p.get(k) for k in ("productId", "productName", "brand", "categories", "categoryId", "link")}
                                  | {"items": [{"itemId": i.get("itemId"), "name": i.get("name"), "ean": i.get("ean"),
                                                "mu": i.get("measurementUnit"), "um": i.get("unitMultiplier"),
                                                "sellers": [{"id": s.get("sellerId"),
                                                             "of": {k: (s.get("commertialOffer") or {}).get(k) for k in
                                                                    ("Price", "ListPrice", "PriceWithoutDiscount", "AvailableQuantity", "IsAvailable")}}
                                                            for s in (i.get("sellers") or [])[:2]]}
                                               for i in (p.get("items") or [])[:2]]} for p in j[:2]]
            else:
                out["search_texto"] = r2.text[:300]
        else:
            out["tree_texto"] = r.text[:200]
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def shopify(host):
    try:
        r = requests.get(f"https://{host}/products.json", headers=H, params={"limit": 2}, timeout=30)
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            p = r.json().get("products", [])
            return {"ok": True, "ejemplo": [{k: x.get(k) for k in ("id", "title", "vendor", "product_type", "tags")}
                                            | {"variants": [{k: v.get(k) for k in ("id", "title", "price", "compare_at_price", "available", "sku")}
                                                            for v in (x.get("variants") or [])[:2]]} for x in p[:2]]}
        return {"ok": False, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}


def sondear(host):
    v = vtex(host)
    res = {"host": host, "robots": robots(host), "vtex": v}
    if "tree" not in v:
        res["shopify"] = shopify(host)
    return res


if __name__ == "__main__":
    hosts = [h.strip() for h in open(sys.argv[1], encoding="utf-8") if h.strip() and not h.startswith("#")]
    with ThreadPoolExecutor(16) as ex:
        res = list(ex.map(sondear, hosts))
    json.dump(res, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
    for r in res:
        v = r["vtex"]
        print(r["host"], "VTEX" if "tree" in v else "-", v.get("search_status"), v.get("resources"),
              "robots_api=", r["robots"].get("api"), "shopify=", (r.get("shopify") or {}).get("ok"))
