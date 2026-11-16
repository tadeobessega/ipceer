"""Línea de comandos: `ipceer <comando>`.

Comandos:
  descargar              archiva los zips SEPA publicados que falten (y las copias de la Wayback Machine)
  procesar               arma el panel diario de los zips archivados sin procesar
  consolidar             arma/actualiza los paneles semanales y mensuales
  calcular               calcula los índices semanal y mensual y escribe resultados/
  sitio                  construye sitio/index.html con los resultados
  diario                 todo lo anterior, en orden (es lo que corre la tarea programada)
  reporte-clasificacion  lista los productos sin clasificar más frecuentes
  validar --indec CSV    compara las divisiones con el IPC del INDEC
  indec                  baja las series del IPC del INDEC a data/indec
  historico              procesa la base 2016-2018 de Precios Claros (Zenodo)
"""

from __future__ import annotations

import argparse
import gc
import json
from datetime import date
import logging
import sys
from pathlib import Path

import polars as pl

from . import combustibles, diario, servicios, otras_fuentes, externos, geo, descarga, hilo, municipios, historico, indec, indicadores, indice, lectura, panel, resumen, sitio, validacion
from .clasificador import SIN_CLASIFICAR
from .config import cargar_config

log = logging.getLogger("ipceer")


def cmd_descargar(cfg, args):
    try:
        descarga.descargar_wayback(cfg)
    except Exception as e:  # la Wayback Machine es un extra: si falla, sigue el día normal
        log.warning("Wayback Machine no disponible: %s", e)
    try:
        pend = descarga.descargar_pendientes(cfg)
    except Exception as e:  # si el portal no responde, igual se completa desde el respaldo
        log.error("Portal de SEPA: %s", e)
    try:
        n = descarga.desde_github(cfg)
        if n:
            log.info("Respaldo en GitHub: %d días recuperados", len(n))
    except Exception as e:
        log.warning("Respaldo en GitHub: %s", e)
    pend = descarga.zips_sin_procesar(cfg)
    log.info("Zips archivados pendientes de procesar: %d", len(pend))


def cmd_procesar(cfg, args):
    # Plan A (D-33): primero se traen los días que GitHub Actions ya procesó; la PC procesa sola solo
    # los zips que GitHub no procesó después de `procesar_local_tras_dias` días (o todos si es 0).
    try:
        n = descarga.procesados_desde_github(cfg)
        log.info("Días procesados en GitHub y traídos: %d", len(n))
    except Exception as e:
        log.warning("Procesados en GitHub: %s", e)
    espera = int((cfg["descarga"].get("procesar_local_tras_dias") or 0))
    limite = date.today().toordinal() - espera
    for z in descarga.zips_sin_procesar(cfg):
        if espera and descarga.fecha_de_zip(z).toordinal() > limite:
            log.info("%s: se espera a que lo procese GitHub", z.name)
            continue
        try:
            lectura.procesar_zip_diario(z, descarga.fecha_de_zip(z), cfg)
        except Exception as e:
            log.error("No se pudo procesar %s: %s", z.name, e)


def cmd_carrera(cfg, args):
    from . import carrera
    if args.historico:
        h = carrera.historico(cfg, solo=[x for x in args.variante.split(",") if x] or None, forzar=args.forzar)
        for k, v in h.items():
            log.info("Histórico %s: contra la réplica %s; contra el nivel general %s", k, v.get("vs_replica"), v.get("vs_general"))
        return
    r = carrera.actual(cfg)
    for v in r["variantes"]:
        log.info("Carrera %s: %s", v["id"], [(m["mes"], m["var"], "parcial" if m["parcial"] else "") for m in v.get("meses", [])])


def cmd_web(cfg, args):
    from . import web
    solo = [x for x in args.tiendas.split(",") if x] or None
    parte = tuple(int(x) for x in args.parte.split("/")) if args.parte else None
    r = web.actualizar(cfg, bajar_ahora=not args.sin_bajar, prefijo="gh_" if args.solo_bajar else "", solo=solo,
                       parte=parte)
    d = r.get("descarga") or {}
    log.info("Web: %s artículos de %s tiendas", d.get("articulos"), d.get("tiendas_con_datos"))
    for tid, i in (d.get("tiendas") or {}).items():
        if not i.get("articulos") or i.get("estado") != "ok":
            log.info("  %s: %s (%s artículos)", tid, i.get("estado"), i.get("articulos"))
    if r.get("panel"):
        log.info("Web, paneles: %s", {k: v for k, v in r["panel"].items() if k != "clases"})


def cmd_consolidar(cfg, args):
    for f in ("semanal", "mensual"):
        panel.actualizar_paneles(cfg, f, todos=getattr(args, "todos", False))


def _datos_fuentes(cfg, datos: dict) -> None:
    """Partes del tablero que salen de otras fuentes y regulados (baratas de recalcular): las arma
    `calcular` y las refresca `sitio`, así una corrida de otras fuentes se ve sin recalcular el índice."""
    try:
        ra = cfg.ruta("resultados") / "alimentos_web.json"
        if ra.exists():
            datos["alimentos_web"] = json.loads(ra.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Alimentos web: %s", e)
    try:
        datos["originales"] = resumen.originales(cfg)
    except Exception as e:
        log.warning("Originales: %s", e)
    try:
        ro = cfg.ruta("resultados") / "otras_fuentes.json"
        if ro.exists():
            datos["otras"] = json.loads(ro.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Otras fuentes: %s", e)
    try:
        from . import normas, prepagas
        pp = prepagas.resumen(cfg)
        ind = validacion.leer_indec(indec.directorio(cfg)).filter(pl.col("codigo") == "06.4.1")
        ref = {}
        for (reg,), g in ind.group_by(["region"]):
            x = dict(g.select("mes", "indice").iter_rows())
            ref[reg] = {m: round((x[m] / x[p] - 1) * 100, 3) for m in x
                        if (p := f"{int(m[:4]) - (m[5:] == '01')}-{12 if m[5:] == '01' else int(m[5:]) - 1:02d}") in x}
        pp["indec"] = ref
        datos["prepagas"] = resumen._limpio(pp)
        rn = normas._dir(cfg) / "normas.csv"
        if rn.exists():
            datos["normas"] = pl.read_csv(rn, infer_schema_length=0).sort("id", descending=True).head(60).to_dicts()
        from . import boletines
        bol = []
        for nombre in ("normas_caba.csv", "normas_pba.csv"):
            rb = boletines._dir(cfg) / nombre
            if rb.exists():
                for n in pl.read_csv(rb, infer_schema_length=0).to_dicts():
                    if n.get("jurisdiccion") == "Ciudad":   # se reclasifica con las reglas vigentes
                        n["rubro"] = boletines.rubro_caba(n.get("nombre") or "", n.get("sumario") or "")
                    if n.get("rubro") and n["rubro"] != "otras":
                        bol.append(n)
        # las más recientes primero (fecha dd/mm/aaaa)
        bol.sort(key=lambda n: (n.get("fecha") or "")[6:] + (n.get("fecha") or "")[3:5] + (n.get("fecha") or "")[:2], reverse=True)
        datos["normas_boletines"] = bol[:60]
    except Exception as e:
        log.warning("Prepagas y normas: %s", e)
    try:
        datos["afispop"] = otras_fuentes.afispop(cfg)
    except Exception as e:
        log.warning("Canasta del IIEP: %s", e)
    try:
        datos["servicios"] = resumen._limpio(externos.canasta_servicios(cfg))
    except Exception as e:
        log.warning("Canasta de servicios: %s", e)
    try:
        datos["regulados"] = resumen._limpio(externos.datos_regulados(cfg))
        datos["externos"] = resumen._limpio(externos.externos(cfg))
        datos["errores_consultoras"] = resumen._limpio(externos.errores_consultoras(cfg))
        datos["tarifas_reales"] = resumen._limpio(externos.tarifas_reales(cfg))
        datos["indec_var"] = resumen._limpio(externos.indec_variaciones(cfg))
        datos["cobertura_canasta"] = resumen.canasta_cobertura(cfg)
    except Exception as e:
        log.warning("Regulados y consultoras: %s", e)
    try:
        # series oficiales nacionales para la serie empalmada (se refrescan apenas el INDEC publica)
        ind = validacion.leer_indec(indec.directorio(cfg)).filter(
            (pl.col("region") == "Nacional") & pl.col("codigo").is_in(["0", "B", "01"]))
        datos["indec"] = {c: [[m, round(i, 4)] for m, i in g.sort("mes").select("mes", "indice").iter_rows()]
                          for (c,), g in ind.group_by(["codigo"])}
    except Exception as e:
        log.warning("Series del INDEC para el empalme: %s", e)


def cmd_alimentos_web(cfg, args):
    """D-54 en un proceso aparte: el oficial sale de los índices ya guardados."""
    res = {}
    for f in ("semanal", "mensual"):
        r = cfg.ruta("resultados") / f"indices_{f}.csv"
        if r.exists():
            res[f] = {"indices": pl.read_csv(r, schema_overrides={"codigo": pl.Utf8, "periodo": pl.Utf8}, infer_schema_length=None)}
    _alimentos_web(cfg, res)


def _alimentos_web(cfg, res: dict) -> None:
    """Alternativa (no publicada): alimentos y bebidas (división 01) sumando los precios web (frescos
    por kilo de las tiendas en línea), al lado del oficial y del INDEC. resultados/alimentos_web.json."""
    from .indec import directorio
    from .validacion import leer_indec
    out = {"nota": "Alternativa sin publicar: alimentos con los precios de las tiendas en línea (D-54)."}
    try:
        ind = leer_indec(directorio(cfg))
    except Exception:
        ind = None
    for f in ("semanal", "mensual"):
        if f not in res:
            continue
        alt = indice.calcular(cfg, f, web=True)["indices"]
        filas = []
        for reg in ("GBA", indice.NACIONAL):
            sel = lambda d: {p: v for p, v in d.filter((pl.col("nivel") == "division") & (pl.col("codigo") == "01")
                                                         & (pl.col("region") == reg)).select("periodo", "var_pct").iter_rows()
                             if v is not None and v == v}
            a, o = sel(alt), sel(res[f]["indices"])
            ref = {}
            if f == "mensual" and ind is not None and not ind.is_empty():
                g = dict(ind.filter((pl.col("codigo") == "01") & (pl.col("region") == reg)).select("mes", "indice").iter_rows())
                ms = sorted(g)
                ref = {m: (g[m] / g[ms[i - 1]] - 1) * 100 for i, m in enumerate(ms) if i}
            for p in sorted(set(a) | set(o)):
                filas.append({"region": reg, "periodo": p, "con_web": a.get(p), "oficial": o.get(p), "indec": ref.get(p)})
        out[f] = filas
    (cfg.ruta("resultados") / "alimentos_web.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def cmd_calcular(cfg, args):
    res = {}
    for f in ("semanal", "mensual"):
        try:
            iw = (cfg["indice"] or {}).get("web") or {}  # D-52: tiendas en línea en el índice publicado
            isv = (cfg["indice"] or {}).get("servicios") or {}  # D-53: tarifas, alquiler (ICL) y prepagas
            ext = servicios.relativos(cfg, f, tuple(isv.get("fuentes") or servicios.FUENTES)) if isv.get("usar") else None
            kw = dict(web=bool(iw.get("usar")), web_excluir=tuple(iw.get("excluir_divisiones") or ()))
            if isv.get("restaurantes"):  # D-55: primero los bienes solos, para imputar restaurantes con ellos
                b = indice.calcular(cfg, f, **kw)["indices"]
                rr = servicios.restaurantes(b)
                del b
                gc.collect()
                ext = rr if ext is None or ext.is_empty() else pl.concat([ext, rr])
            r = indice.calcular(cfg, f, **kw, extra=ext)
        except (ValueError, FileNotFoundError) as e:
            log.warning("Índice %s: %s", f, e)
            continue
        indice.guardar(r, cfg, f)
        res[f] = r
    if not res:
        return
    # D-54: la alternativa de alimentos con web corre en otro proceso, para no sumar su memoria a la
    # del cálculo principal (el runner de GitHub se quedaba sin memoria y se cortaba sin log)
    try:
        import subprocess
        subprocess.run([sys.executable, "-m", "ipceer.cli", "alimentos-web"], check=False, timeout=1800)
    except Exception as e:
        log.warning("Alimentos con web (alternativa): %s", e)
    val, cba_m = {}, None
    try:
        cba_m = indicadores.cba_mensual(cfg)
        if not cba_m.is_empty():
            cba_m.write_csv(cfg.ruta("resultados") / "cba_mensual.csv")
    except Exception as e:
        log.warning("CBA mensual: %s", e)
    comb_datos, comb_idx = {}, None
    try:
        comb_datos, comb_idx = combustibles.construir(cfg)
    except Exception as e:
        log.warning("Combustibles: %s", e)
    reg_idx = None
    try:
        reg_idx = externos.indice_regulados(cfg)
    except Exception as e:
        log.warning("Regulados: %s", e)
    if "mensual" in res:
        try:
            mens_val = res["mensual"]["indices"]
            if reg_idx is not None:
                mens_val = pl.concat([mens_val, reg_idx.select([c for c in reg_idx.columns if c in mens_val.columns])],
                                     how="diagonal_relaxed")
            if comb_idx is not None:
                mens_val = pl.concat([mens_val, comb_idx.select([c for c in comb_idx.columns if c in mens_val.columns])],
                                     how="diagonal_relaxed")
            val = validacion.construir(cfg, mens_val, res.get("semanal", {}).get("indices"),
                                       indec.directorio(cfg), cba_propia=cba_m)
        except Exception as e:
            log.warning("Validación INDEC: %s", e)
    ind = {}
    try:
        ind = indicadores.calcular(cfg)
        indicadores.guardar(ind, cfg)
    except Exception as e:
        log.warning("Indicadores: %s", e)
    sem_ind = res.get("semanal", {}).get("indices")
    datos = resumen.armar_datos(
        cfg,
        res.get("mensual", {}).get("indices"),
        sem_ind,
        (res.get("semanal") or res.get("mensual"))["cobertura"],
        validacion=val,
        indicadores=ind,
    )
    if comb_datos:
        try:
            comb_datos["total_con_combustibles"] = combustibles.total_con_combustibles(
                cfg, res.get("mensual", {}).get("indices"), comb_idx, res.get("mensual", {}).get("cobertura"))
        except Exception as e:
            log.warning("IPCEER con combustibles: %s", e)
        datos["combustibles"] = resumen._limpio(comb_datos)
    try:
        cba_of = indec.leer_cba(indec.directorio(cfg))
        of = dict(cba_of.select("mes", "cba").iter_rows()) if cba_of is not None and not cba_of.is_empty() else {}
        can = indicadores.canasta_mensual(cfg, oficial=of)
        datos["canastas"] = resumen._limpio({
            "mensual": can.to_dicts() if not can.is_empty() else [],
            "componentes": indicadores.componentes_cba(cfg),
            "indec": cba_of.to_dicts() if cba_of is not None and not cba_of.is_empty() else []})
        if not can.is_empty():
            can.write_csv(cfg.ruta("resultados") / "canasta_mensual.csv")
    except Exception as e:
        log.warning("Canastas: %s", e)
    try:
        # series oficiales nacionales para la serie empalmada (INDEC hasta su último mes, después IPCEER)
        ind = validacion.leer_indec(indec.directorio(cfg)).filter(
            (pl.col("region") == "Nacional") & pl.col("codigo").is_in(["0", "B", "01"]))
        datos["indec"] = {c: [[m, round(i, 4)] for m, i in g.sort("mes").select("mes", "indice").iter_rows()]
                          for (c,), g in ind.group_by(["codigo"])}
    except Exception as e:
        log.warning("Series del INDEC para el empalme: %s", e)
    try:
        rv = cfg.ruta("resultados") / "variantes.json"
        if rv.exists():
            datos["variantes"] = json.loads(rv.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Variantes: %s", e)
    try:
        rc = cfg.ruta("resultados") / "carrera.json"
        if rc.exists():
            datos["carrera"] = json.loads(rc.read_text(encoding="utf-8"))
        rp = cfg.ruta("resultados") / "pronosticos.csv"
        if rp.exists():
            datos["pronosticos"] = pl.read_csv(rp, infer_schema_length=None).to_dicts()
        rh = cfg.ruta("resultados") / "carrera_historico.json"
        if rh.exists():
            datos["carrera_historico"] = {k: {x: v[x] for x in ("meses", "vs_general", "vs_replica", "calculado")}
                                          for k, v in json.loads(rh.read_text(encoding="utf-8")).items()}
    except Exception as e:
        log.warning("Carrera de métodos: %s", e)
    _datos_fuentes(cfg, datos)
    try:
        datos["municipios"] = resumen._limpio(municipios.calcular(cfg))
    except Exception as e:
        log.warning("Municipios: %s", e)
    try:
        geo.actualizar(cfg)
        g = geo.datos(cfg)
        if g:
            if "combustibles" not in datos:
                g["geometria"] = combustibles.geometria_simple(cfg)
            datos["geo"] = resumen._limpio(g)
    except Exception as e:
        log.warning("Mapa por provincia y departamento: %s", e)
    try:
        datos["diario"] = resumen._limpio(diario.calcular(cfg))
    except Exception as e:
        log.warning("Índice diario: %s", e)
    try:
        if sem_ind is not None and "tramo" in sem_ind.columns:
            sem_ind = sem_ind.filter(pl.col("tramo") == sem_ind["tramo"].max())
        h = hilo.armar_todos(cfg, datos, sem_ind, datos.get("indicadores", {}))
        hilo.guardar(h, cfg)
        datos["hilo"] = resumen._limpio(h)
    except Exception as e:
        log.warning("Hilo: %s", e)
    (cfg.ruta("resultados") / "datos.json").write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    try:
        log.info("Registro de lo publicado (vintages.csv): %d valores nuevos o revisados", resumen.registrar_vintages(cfg, datos))
    except Exception as e:
        log.warning("Registro de lo publicado: %s", e)


def cmd_sitio(cfg, args):
    ruta = cfg.ruta("resultados") / "datos.json"
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    _datos_fuentes(cfg, datos)
    resumen.recortar_series(datos, cfg)
    if True:  # el hilo se rearma sobre las series recortadas (ej. sin nivel general antes de tiempo)
        try:
            datos["hilo"] = resumen._limpio(hilo.armar_todos(cfg, datos, None, datos.get("indicadores", {})))
        except Exception as e:
            log.warning("Hilo: %s", e)
    ruta.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    destino = sitio.construir(cfg, datos)
    log.info("Sitio: %s", destino)


def cmd_diario(cfg, args):
    try:
        indec.descargar(cfg)
    except Exception as e:
        log.warning("INDEC: %s", e)
    try:
        cmd_descargar(cfg, args)
    except Exception as e:  # sin red igual se procesa lo que ya está archivado
        log.error("Descarga fallida: %s", e)
    cmd_procesar(cfg, args)
    try:
        combustibles.actualizar(cfg)
    except Exception as e:
        log.error("Combustibles: %s", e)
    if panel.dias_disponibles(cfg):
        cmd_consolidar(cfg, args)
        cmd_calcular(cfg, args)
        if (cfg.ruta("resultados") / "datos.json").exists():
            cmd_sitio(cfg, args)



# partes del tablero que GitHub no puede recalcular (necesitan la historia completa de la PC):
# se toman del último datos.json de la PC
DE_LA_PC = ("validacion", "carrera", "carrera_historico", "variantes", "pronosticos", "combustibles")


def cmd_telefonia(cfg, args):
    """Relevamiento diario de planes de telefonía e internet (D-57)."""
    from . import telefonia
    df = telefonia.relevar(cfg)
    if not df.is_empty():
        with pl.Config(tbl_rows=200, fmt_str_lengths=90, tbl_width_chars=250):
            print(df.select("empresa", "clase", "precio", "antes", "sin_impuestos", "promo", "contexto", "linea"))
    print(json.dumps(telefonia.resumen(cfg), ensure_ascii=False, indent=1))


def cmd_github(cfg, args):
    """Tablero calculado en GitHub, sin la PC (decisión D-49).

    GitHub tiene los días procesados desde el 1/11/2026 (proc_*.tar del release) y, en la rama
    datos-base, los paneles de los períodos cerrados antes que no puede rehacer. Con eso calcula el
    tramo publicado igual que la PC; lo que necesita la historia completa (validación 2016-2018,
    carrera de métodos, combustibles) sale del último datos.json de la PC (`--base`)."""
    from pathlib import Path
    try:
        indec.descargar(cfg)
    except Exception as e:
        log.warning("INDEC: %s", e)
    from . import descarga
    log.info("Días procesados traídos de GitHub: %d", len(descarga.procesados_desde_github(cfg)))
    try:
        from . import web
        web.actualizar(cfg, bajar_ahora=False)
    except Exception as e:
        log.warning("Precios web: %s", e)
    base = {}
    if args.base and Path(args.base).exists():
        base = json.loads(Path(args.base).read_text(encoding="utf-8"))
    # combustibles (D-56): GitHub no tiene el panel de Energía; el índice 07.2.2 sale del de la PC
    rc = cfg.ruta("resultados") / "combustibles_mensual.csv"
    ci = (base.get("combustibles") or {}).get("indice") or []
    if not rc.exists() and ci:
        pl.DataFrame([{"periodo": x["mes"], "region": x["region"], "nivel": "clase", "codigo": "07.2.2",
                       "indice": x.get("indice"), "var_pct": x.get("var")} for x in ci]).write_csv(rc)
    cmd_consolidar(cfg, args)
    cmd_calcular(cfg, args)
    ruta = cfg.ruta("resultados") / "datos.json"
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    for k in DE_LA_PC:
        if k in base:
            datos[k] = base[k]
    for k, v in base.items():
        if k not in datos or datos[k] in (None, {}, []):
            datos[k] = v
    datos["origen"] = {"calculado_en": "github", "base_pc": base.get("generado")}
    ruta.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    cmd_sitio(cfg, args)

def cmd_cadenas(cfg, args):
    log.info("Cadenas con nombre: %d", lectura.catalogo_cadenas(cfg))


def cmd_estado(cfg, args):
    """Resumen de qué datos hay y qué salió del último cálculo (resultados/estado.json)."""
    from datetime import datetime
    res = cfg.ruta("resultados")
    e = {"generado": datetime.now().isoformat(timespec="seconds")}
    dias = panel.dias_disponibles(cfg)
    e["sepa_dias"] = len(dias)
    e["sepa_desde"], e["sepa_hasta"] = (dias[0].isoformat(), dias[-1].isoformat()) if dias else (None, None)
    e["semanas"] = len(list(cfg.ruta("panel_semanal").glob("*.parquet")))
    e["meses"] = len(list(cfg.ruta("panel_mensual").glob("*.parquet")))
    e["zips_archivados"] = len(list(cfg.ruta("crudos").glob("sepa_*.zip")))
    cad = cfg.ruta("panel_diario").parent / "cadenas.csv"
    e["cadenas_con_nombre"] = pl.read_csv(cad).height if cad.exists() else 0
    try:
        e["cba_indec_meses"] = indec.leer_cba(indec.directorio(cfg)).height
    except Exception as x:
        e["cba_indec_meses"] = f"error: {x}"
    pc = combustibles.directorio(cfg) / "panel.parquet"
    if pc.exists():
        p = pl.read_parquet(pc)
        e["combustibles"] = {"filas": p.height, "estaciones": p["est"].n_unique(), "desde": p["mes_id"].min(),
                             "hasta": p["mes_id"].max(), "sin_departamento_pct": round(100 * p["depto_id"].null_count() / max(p.height, 1), 2)}
    else:
        e["combustibles"] = "sin datos"
    e["surtidor_copias"] = len(list((combustibles.directorio(cfg) / "surtidor").glob("*.csv.gz")))
    dj = res / "datos.json"
    if dj.exists():
        d = json.loads(dj.read_text(encoding="utf-8"))
        ind = d.get("indicadores", {})
        e["indicadores"] = {k: len(v) if isinstance(v, list) else bool(v) for k, v in ind.items() if k != "parametros"}
        e["hilo_semana"] = (d.get("hilo") or {}).get("semana")
        e["validacion_pares"] = len((d.get("validacion") or {}).get("pares", []))
        e["validacion_cba"] = any(p.get("codigo") == "CBA" for p in (d.get("validacion") or {}).get("pares", []))
        e["combustibles_en_tablero"] = bool(d.get("combustibles"))
    (res / "estado.json").write_text(json.dumps(e, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(e, ensure_ascii=False, indent=2))


def cmd_indec(cfg, args):
    indec.descargar(cfg, max_horas=0 if getattr(args, "forzar", False) else 24)


def cmd_historico(cfg, args):
    from datetime import date
    n = historico.construir(cfg, date.fromisoformat(args.desde) if args.desde else None,
                            date.fromisoformat(args.hasta) if args.hasta else None, args.lote)
    faltan = sum(1 for d in historico.listar_dias() if not (cfg.ruta("panel_diario") / f"{d.isoformat()}.parquet").exists())
    log.info("Zenodo: %d días nuevos; faltan %d", n, faltan)
    (cfg.ruta("resultados") / "historico_estado.json").write_text(
        json.dumps({"procesados_en_este_lote": n, "faltan": faltan}), encoding="utf-8")


def cmd_reporte(cfg, args):
    rutas = sorted(cfg.ruta("panel_mensual").glob("*.parquet"))
    if not rutas:
        sys.exit("No hay paneles mensuales todavía")
    df = pl.read_parquet(rutas[-1])
    tot = df.height
    sin = df.filter(pl.col("clase") == SIN_CLASIFICAR)
    print(f"{rutas[-1].stem}: {tot} productos-cadena-región, {100 * sin.height / tot:.1f}% sin clasificar")
    print(df.group_by("clase").len().sort("len", descending=True))
    top = sin.group_by("descripcion").agg(pl.len().alias("n")).sort("n", descending=True).head(args.n)
    destino = cfg.ruta("resultados") / "sin_clasificar.csv"
    top.write_csv(destino)
    print(top.head(40))
    print(f"Lista completa en {destino}")


def cmd_validar(cfg, args):
    ind = pl.read_csv(cfg.ruta("resultados") / "indices_mensual.csv", schema_overrides={"codigo": pl.Utf8})
    ind = ind.rename({"periodo": "mes", "var_pct": "var_mensual_pct"})
    comp = validacion.comparar(ind, Path(args.indec))
    destino = cfg.ruta("resultados") / "validacion_indec.csv"
    comp.write_csv(destino)
    print(comp)
    print(f"Guardado en {destino}")


def main(argv=None):
    import os
    if os.environ.get("IPCEER_TRAZA"):  # diagnóstico: pila de todos los hilos si el proceso se cuelga
        import faulthandler
        faulthandler.dump_traceback_later(int(os.environ["IPCEER_TRAZA"]), repeat=True)
    p = argparse.ArgumentParser(prog="ipceer", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--raiz", help="raíz del repo (por defecto, el directorio actual)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    for nombre, fn in [("descargar", cmd_descargar), ("procesar", cmd_procesar), ("calcular", cmd_calcular),
                       ("sitio", cmd_sitio), ("diario", cmd_diario)]:
        sub.add_parser(nombre).set_defaults(fn=fn)
    c = sub.add_parser("consolidar")
    c.add_argument("--todos", action="store_true", help="rehace todos los períodos (ej. tras cambiar el clasificador)")
    c.set_defaults(fn=cmd_consolidar)
    sub.add_parser("combustibles", help="baja y procesa los precios de combustibles de Energía").set_defaults(
        fn=lambda cfg, args: combustibles.actualizar(cfg))
    sub.add_parser("cadenas", help="nombres de las cadenas desde los últimos zips").set_defaults(fn=cmd_cadenas)
    sub.add_parser("alimentos-web", help="alternativa de alimentos con los precios web (D-54)").set_defaults(fn=cmd_alimentos_web)
    sub.add_parser("telefonia", help="planes de telefonía e internet de los sitios de las empresas (D-57)").set_defaults(fn=cmd_telefonia)
    g = sub.add_parser("github", help="calcula el tablero en GitHub, sin la PC (D-49)")
    g.add_argument("--base", default="", help="último datos.json de la PC (partes que GitHub no recalcula)")
    g.set_defaults(fn=cmd_github)
    sub.add_parser("estado", help="resumen de datos y resultados").set_defaults(fn=cmd_estado)
    w = sub.add_parser("web", help="precios minoristas de tiendas en línea (catálogos públicos)")
    w.add_argument("--solo-bajar", action="store_true", help="baja y guarda con prefijo gh_ (GitHub Actions)")
    w.add_argument("--sin-bajar", action="store_true", help="solo rearma los paneles con lo ya bajado")
    w.add_argument("--tiendas", default="", help="ids separados por coma (por defecto, todas)")
    w.add_argument("--parte", default="", help="i/n: solo las tiendas que le tocan a la corrida i de n")
    w.set_defaults(fn=cmd_web)
    c = sub.add_parser("carrera", help="todas las variantes de método, contra el INDEC y las consultoras")
    c.add_argument("--historico", action="store_true", help="prueba 2016-2018 de las variantes que falten")
    c.add_argument("--variante", default="", help="con --historico: solo estas (ids separados por coma)")
    c.add_argument("--forzar", action="store_true")
    c.set_defaults(fn=cmd_carrera)
    o = sub.add_parser("otras", help="alquileres, medicamentos, frescos, BCRA, prepagas y normas de tarifas")
    o.add_argument("--desde-github", action="store_true", help="traer lo que ya bajó GitHub Actions (Plan A)")
    o.set_defaults(fn=lambda cfg, a: otras_fuentes.actualizar(cfg, desde_gh=a.desde_github))
    sub.add_parser("variantes", help="compara variantes de método contra el INDEC (2016-2018)").set_defaults(
        fn=lambda cfg, a: __import__("runpy").run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "variantes.py"), run_name="__main__"))
    sub.add_parser("municipios", help="paneles e índices por municipio").set_defaults(
        fn=lambda cfg, args: log.info("Municipios: %d días reprocesados", municipios.reprocesar(cfg)))
    i = sub.add_parser("indec")
    i.add_argument("--forzar", action="store_true")
    i.set_defaults(fn=cmd_indec)
    h = sub.add_parser("historico", help="base 2016-2018 de Precios Claros en Zenodo")
    h.add_argument("--desde")
    h.add_argument("--hasta")
    h.add_argument("--lote", type=int, help="procesa a lo sumo esta cantidad de días y termina")
    h.set_defaults(fn=cmd_historico)
    r = sub.add_parser("reporte-clasificacion")
    r.add_argument("-n", type=int, default=500)
    r.set_defaults(fn=cmd_reporte)
    v = sub.add_parser("validar")
    v.add_argument("--indec", required=True, help="ruta a serie_ipc_divisiones.csv del INDEC")
    v.set_defaults(fn=cmd_validar)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = cargar_config(Path(args.raiz) if args.raiz else None)
    args.fn(cfg, args)


if __name__ == "__main__":
    main()
