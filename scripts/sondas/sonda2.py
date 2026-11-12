"""Sonda de fuentes de transporte, vivienda y autos (no guarda nada)."""
import io, re, sys, requests
UA = {"User-Agent": "IPCEER/1.0 (+https://github.com/tadeobessega/ipceer)"}
def get(u, n=600):
    try:
        r = requests.get(u, headers=UA, timeout=40, allow_redirects=True)
        t = r.text if "pdf" not in r.headers.get("content-type", "") else f"<PDF {len(r.content)} bytes>"
        print(f"\n### {r.status_code} {r.url} [{r.headers.get('content-type','')}] {len(r.content)}")
        print(re.sub(r"\s+", " ", re.sub(r"(?s)<script.*?</script>|<style.*?</style>|<[^>]+>", " ", t))[:n])
        return r
    except Exception as e:
        print(f"\n### ERR {u}: {e}")
for host in ["https://www.argentina.gob.ar", "https://buenosaires.gob.ar", "https://www.gba.gob.ar", "https://boletinoficial.buenosaires.gob.ar",
             "https://normas.gba.gob.ar", "https://www.aysa.com.ar", "https://www.cca.org.ar", "https://www.zonaprop.com.ar", "https://www.metrogas.com.ar",
             "https://www.naturgy.com.ar", "https://emova.com.ar", "https://www.trenesargentinos.gob.ar", "https://www.acara.org.ar"]:
    get(host + "/robots.txt", 900)
for u in ["https://www.argentina.gob.ar/transporte/tarifas-del-transporte-publico-en-el-amba",
          "https://www.argentina.gob.ar/sube/tarifas", "https://www.argentina.gob.ar/transporte/sube/tarifas",
          "https://www.argentina.gob.ar/transporte/trenes-argentinos/tarifas",
          "https://buenosaires.gob.ar/movilidad/transporte-publico/tarifas",
          "https://buenosaires.gob.ar/subte/tarifas", "https://www.gba.gob.ar/transporte/tarifas",
          "https://www.gba.gob.ar/transporte/cuadros_tarifarios",
          "https://www.aysa.com.ar/usuarios/tarifas", "https://www.aysa.com.ar/usuarios/Cuadro-tarifario",
          "https://www.cca.org.ar/precios.php", "https://www.cca.org.ar/",
          ]:
    get(u, 1500)
for a, m in [(2026, 10), (2026, 9)]:
    s = (a, m + 1) if m < 12 else (a + 1, 1)
    for n in ["INDEX_GBA_NORTE_REPORTE", "INDEX_GBA_OESTE_REPORTE", "INDEX_GBA_SUR_REPORTE", "INDEX_GBA_OESTE_SUR_REPORTE", "INDEX_GBA_REPORTE",
              "INDEX_CORDOBA_REPORTE", "INDEX_ROSARIO_REPORTE", "INDEX_MENDOZA_REPORTE"]:
        u = f"https://www.zonaprop.com.ar/blog/wp-content/uploads/{s[0]}/{s[1]:02d}/{n}_{a}-{m:02d}.pdf"
        try:
            r = requests.head(u, headers=UA, timeout=30, allow_redirects=True); print("HEAD", r.status_code, u)
        except Exception as e:
            print("HEAD ERR", u, e)
from pypdf import PdfReader
B = "https://servicios.infoleg.gob.ar/infolegInternet/anexos/425000-429999"
for nid in (429494, 429497):
    h = requests.get(f"{B}/{nid}/norma.htm", headers=UA, timeout=40)
    pdfs = re.findall(r'href="([^"]+\.pdf)"', h.text, flags=re.I)
    print(f"\n##### Infoleg {nid}: anexos {pdfs}")
    for p in pdfs[:2]:
        r = requests.get(f"{B}/{nid}/{p}", headers=UA, timeout=60)
        if r.content[:4] == b"%PDF":
            t = "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(r.content)).pages)
            print(f"--- {p} ({len(t)} car.)\n{t[:3500]}")
