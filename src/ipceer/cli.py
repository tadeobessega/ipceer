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
import json
import logging
import sys
from pathlib import Path

import polars as pl

from . import combustibles, descarga, hilo, historico, indec, indicadores, indice, lectura, panel, resumen, sitio, validacion
from .clasificador import SIN_CLASIFICAR
from .config import cargar_config

log = logging.getLogger("ipceer")


def cmd_descargar(cfg, args):
    try:
        descarga.descargar_wayback(cfg)
    except Exception as e:  # la Wayback Machine es un extra: si falla, sigue el día normal
        log.warning("Wayback Machine no disponible: %s", e)
    pend = descarga.descargar_pendientes(cfg)
    log.info("Zips archivados pendientes de procesar: %d", len(pend))


def cmd_procesar(cfg, args):
    for z in descarga.zips_sin_procesar(cfg):
        try:
            lectura.procesar_zip_diario(z, descarga.fecha_de_zip(z), cfg)
        except Exception as e:
            log.error("No se pudo procesar %s: %s", z.name, e)


def cmd_consolidar(cfg, args):
    for f in ("semanal", "mensual"):
        panel.actualizar_paneles(cfg, f, todos=getattr(args, "todos", False))


def cmd_calcular(cfg, args):
    res = {}
    for f in ("semanal", "mensual"):
        try:
            r = indice.calcular(cfg, f)
        except (ValueError, FileNotFoundError) as e:
            log.warning("Índice %s: %s", f, e)
            continue
        indice.guardar(r, cfg, f)
        res[f] = r
    if not res:
        return
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
    if "mensual" in res:
        try:
            mens_val = res["mensual"]["indices"]
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
        if sem_ind is not None and "tramo" in sem_ind.columns:
            sem_ind = sem_ind.filter(pl.col("tramo") == sem_ind["tramo"].max())
        h = hilo.armar(cfg, datos, sem_ind, datos.get("indicadores", {}))
        hilo.guardar(h, cfg)
        datos["hilo"] = resumen._limpio(h)
    except Exception as e:
        log.warning("Hilo: %s", e)
    (cfg.ruta("resultados") / "datos.json").write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")


def cmd_sitio(cfg, args):
    datos = json.loads((cfg.ruta("resultados") / "datos.json").read_text(encoding="utf-8"))
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
                            date.fromisoformat(args.hasta) if args.hasta else None)
    log.info("Zenodo: %d días nuevos", n)


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
    sub.add_parser("estado", help="resumen de datos y resultados").set_defaults(fn=cmd_estado)
    i = sub.add_parser("indec")
    i.add_argument("--forzar", action="store_true")
    i.set_defaults(fn=cmd_indec)
    h = sub.add_parser("historico", help="base 2016-2018 de Precios Claros en Zenodo")
    h.add_argument("--desde")
    h.add_argument("--hasta")
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
