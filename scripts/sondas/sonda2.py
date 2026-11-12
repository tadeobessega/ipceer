"""Sonda 3: textos de Zonaprop GBA, gas Naturgy, búsqueda en BO CABA y PBA, lista CCA."""
import io, re, requests, time
from pypdf import PdfReader
UA = {"User-Agent": "IPCEER/1.0 (+https://github.com/tadeobessega/ipceer)"}
def pdftxt(c): return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(c)).pages)
for z in ["GBA_NORTE", "GBA_OESTE", "GBA_SUR"]:
    u = f"https://www.zonaprop.com.ar/blog/wp-content/uploads/2026/10/INDEX_{z}_REPORTE_2026-09.pdf"
    r = requests.get(u, headers=UA, timeout=60)
    t = re.sub(r"\s+", " ", pdftxt(r.content)) if r.content[:4] == b"%PDF" else ""
    print(f"\n##### {z} {r.status_code} {len(t)}")
    for m in re.finditer(r"alquiler", t, re.I):
        print("  ...", t[max(0, m.start()-150): m.start()+250])
B = "https://servicios.infoleg.gob.ar/infolegInternet/anexos/425000-429999"
h = requests.get(f"{B}/429497/norma.htm", headers=UA, timeout=40)
print("\n##### norma 429497 texto:", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h.text))[-1500:])
for p in re.findall(r'href="([^"]+\.pdf)"', h.text, flags=re.I)[:1]:
    r = requests.get(f"{B}/429497/{p}", headers=UA, timeout=60)
    pags = [(pg.extract_text() or "") for pg in PdfReader(io.BytesIO(r.content)).pages]
    print(f"##### Naturgy {p}: {len(pags)} páginas")
    for i, t in enumerate(pags[:3]): print(f"--- pág {i}\n{t[:1800]}")
h = requests.get(f"{B}/429494/norma.htm", headers=UA, timeout=40)
t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h.text))
print("\n##### Metrogas vigencia:", re.findall(r".{0,80}vigencia.{0,120}", t, re.I)[:4])
r = requests.get(f"{B}/429494/res536-1.pdf", headers=UA, timeout=60)
pags = [(pg.extract_text() or "") for pg in PdfReader(io.BytesIO(r.content)).pages]
print(f"##### Metrogas: {len(pags)} páginas; títulos:", [re.findall(r"TARIFAS FINALES[^\n]*", x)[:2] for x in pags])
# BO CABA
s = requests.Session(); s.headers.update(UA)
t0 = time.time(); r = s.get("https://boletinoficial.buenosaires.gob.ar/normativaba", timeout=60)
tok = re.search(r'name="buscador\[_token\]"[^>]*value="([^"]+)"', r.text) or re.search(r'value="([^"]+)"[^>]*name="buscador\[_token\]"', r.text)
org = re.findall(r'<option value="(\d+)"[^>]*>\s*([^<]*TRANSPORTE[^<]*)<', r.text)
print("\n##### BO CABA token", bool(tok), "organismos:", org[:8], round(time.time()-t0, 1))
if tok:
    data = {"buscador[_token]": tok.group(1), "buscador[con_frase_exacta]": "CUADROS TARIFARIOS",
            "buscador[fecha_publicacion_desde]": "2026-08-01", "buscador[busca_en_contenido]": "0"}
    t0 = time.time()
    try:
        r = s.post("https://boletinoficial.buenosaires.gob.ar/normativaba/resultados", data=data, timeout=150)
        print("POST", r.status_code, round(time.time()-t0, 1), len(r.text))
        print(re.findall(r'href="(/normativaba/norma/\d+)"[^>]*>\s*([^<]{0,90})', r.text)[:20])
        tt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text))
        print(tt[tt.find("resultado"):tt.find("resultado")+1500])
    except Exception as e:
        print("POST ERR", e, round(time.time()-t0, 1))
r = requests.get("https://boletinoficial.buenosaires.gob.ar/normativaba/norma/pdf/851638", headers=UA, timeout=60)
if r.content[:4] == b"%PDF":
    t = pdftxt(r.content); i = t.find("ANEXO"); print("\n##### PDF CABA 851638:", len(t), t[max(0,i-100):i+2500] if i >= 0 else t[-2500:])
# PBA
for ph in ["valores tarifarios", "cuadro tarifario", "boleto"]:
    u = ("https://normas.gba.gob.ar/resultados?q%5Bterms%5D%5Braw_type%5D=Resolution&q%5Bphrase%5D=" + requests.utils.quote(ph) +
         "&q%5Bdate_ranges%5D%5Bpublication_date%5D%5Bgte%5D=01%2F01%2F2026")
    r = requests.get(u, headers=UA, timeout=60)
    tt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text))
    print(f"\n##### PBA '{ph}':", re.findall(r"Página 1 de [\d.]+ resultados", tt), [x for x in re.findall(r"Resolución \d+/\d+ del [^R]{0,70}Resumen.{0,200}", tt) if "ransporte" in x][:8])
r = requests.get("https://normas.gba.gob.ar/ar-b/resolucion/2026/122/585820", headers=UA, timeout=60)
print("\n##### PBA 122/26 links:", re.findall(r'href="(/(?:documentos|anexos/descargar)/[^"]+)"', r.text))
for l in re.findall(r'href="(/anexos/descargar/[^"]+\.pdf)"', r.text)[:1]:
    c = requests.get("https://normas.gba.gob.ar" + l, headers=UA, timeout=60).content
    if c[:4] == b"%PDF": print(pdftxt(c)[:2500])
r = requests.get("https://cca.org.ar/", headers=UA, timeout=60)
print("\n##### CCA links:", [x for x in re.findall(r'href="([^"]+)"[^>]*>\s*([^<]{0,40})', r.text) if re.search("precio|lista", x[0]+x[1], re.I)][:15])
