"""Cálculo del índice (mensual o semanal).

Esquema (el mismo que usa el INDEC, adaptado a la fuente):

1. Agregado elemental (código de ponderador x región): media geométrica no ponderada
   de los relativos de precio entre dos períodos consecutivos de los productos
   apareados (mismo producto, cadena, presentación y región en ambos períodos)
   -> índice de Jevons encadenado. Si en una región hay pocos productos apareados,
   se usa el relativo nacional de ese código.
2. Agregación superior: suma ponderada de los índices elementales con los
   ponderadores de la canasta INDEC (base dic-2016, ENGHo 2004/05) de cada región,
   tipo Laspeyres.
3. Índice nacional: suma ponderada de las regiones por su peso en el gasto de
   consumo (Cuadro 6 de la Metodología INDEC 32) y por el peso cubierto en cada una.
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from .clasificador import SIN_CLASIFICAR
from .config import REGIONES, Config, ponderadores_clase, ponderadores_region
from .lectura import CLAVE

log = logging.getLogger(__name__)

NACIONAL = "Nacional"
REZAGO_INTERANUAL = {"mensual": 12, "semanal": 52}


def _siguiente(periodo: str, frecuencia: str) -> str:
    if frecuencia == "mensual":
        a, m = map(int, periodo.split("-"))
        return f"{a + (m == 12)}-{1 if m == 12 else m + 1:02d}"
    a, s = periodo.split("-W")
    lunes = date.fromisocalendar(int(a), int(s), 1) + timedelta(days=7)
    a2, s2, _ = lunes.isocalendar()
    return f"{a2}-W{s2:02d}"


def _inicio(periodo: str, frecuencia: str) -> date:
    if frecuencia == "mensual":
        a, m = map(int, periodo.split("-"))
        return date(a, m, 1)
    a, w = periodo.split("-W")
    return date.fromisocalendar(int(a), int(w), 1)


def tramos(periodos: list[str], frecuencia: str, max_meses: float) -> dict[str, int]:
    """Numera los tramos de la serie: se corta cuando entre dos períodos con datos pasan más
    de `max_meses` (ej. de 2018 a 2025). Dentro de un tramo se encadena aunque haya huecos."""
    out, t = {}, 0
    for i, p in enumerate(periodos):
        if i and (_inicio(p, frecuencia) - _inicio(periodos[i - 1], frecuencia)).days / 30.4375 > max_meses:
            t += 1
        out[p] = t
    return out


def resolver_codigo(clase: str, codigos: set[str], min_niveles: int = 2) -> str | None:
    """Código con ponderador más específico que contiene a la clase.

    No sube más allá del nivel de grupo (2 niveles, ej. "09.3") para no asignarle a
    un puñado de productos el peso de una división entera.
    """
    partes = clase.split(".")
    for i in range(len(partes), min_niveles - 1, -1):
        c = ".".join(partes[:i])
        if c in codigos:
            return c
    return None


def _ruta_web(ruta: Path) -> Path:
    """Panel de precios web del mismo período (data/panel/web_mensual o web_semanal)."""
    return ruta.parent.parent / f"web_{ruta.parent.name}" / ruta.name


def _cargar(ruta: Path, col_precio: str, mapa: dict[str, str | None], web: bool = False,
            estratos: Path | None = None) -> pl.DataFrame:
    """Panel de un período. `estratos`: directorio de config; si se da, agrega la variedad de cada
    artículo (config/variedades.yaml) para la estratificación."""
    cols = [*CLAVE, "clase", col_precio, "n_dias", "dias_con_datos", "n_sucursales"]
    def leer(r: Path) -> pl.DataFrame:
        disp = pl.read_parquet_schema(r)
        c = cols + (["descripcion"] if estratos is not None and "descripcion" in disp else [])
        d = pl.read_parquet(r, columns=c)
        if "descripcion" not in d.columns:
            d = d.with_columns(pl.lit(None, dtype=pl.Utf8).alias("descripcion"))
        return d
    df = leer(ruta).filter(pl.col("clase") != SIN_CLASIFICAR)
    if web and _ruta_web(ruta).exists():
        w = leer(_ruta_web(ruta)).filter(pl.col("clase").is_not_null())
        df = pl.concat([df, w.select(df.columns)], how="vertical_relaxed")
    df = df.select(
        *CLAVE, "clase", pl.col(col_precio).alias("precio"), "n_dias", "dias_con_datos", "descripcion",
        pl.col("n_sucursales").cast(pl.Float64, strict=False).fill_null(1.0).fill_nan(1.0).alias("n_suc"),
    ).with_columns(
        pl.col("clase").replace_strict(mapa, default=None, return_dtype=pl.Utf8).alias("codigo")
    ).filter(pl.col("codigo").is_not_null())
    if estratos is not None:
        from . import variedades
        df = variedades.con_variedad(df, estratos)
    return df.drop("descripcion")


def _en_curso(periodo: str, frecuencia: str, ultimo_dia: date | None) -> bool:
    """Un período es parcial si todavía no terminó: su último día es posterior al último día con
    datos. Un mes o una semana del pasado con días sin archivo (ej. la base 2016-2018, que tiene
    huecos o se baja de a un día por semana) no es parcial: se mide con los días que tiene."""
    if ultimo_dia is None:
        return False
    if frecuencia == "mensual":
        a, m = map(int, periodo.split("-"))
        fin = date(a + (m == 12), 1 if m == 12 else m + 1, 1).toordinal() - 1
    else:
        fin = _inicio(periodo, frecuencia).toordinal() + 6
    return fin > ultimo_dia.toordinal()


def _info(ruta: Path) -> tuple[int, int]:
    d = pl.read_parquet(ruta, columns=["dias_con_datos", "dias_periodo"]).row(0)
    return int(d[0]), int(d[1])


def _min_dias(dias_con_datos: int, minimo: int) -> int:
    return max(1, min(minimo, math.ceil(dias_con_datos / 2)))


def relativos(previo: pl.DataFrame, actual: pl.DataFrame, min_p: int, min_a: int,
              cfg: Config, agregador: str = "media", peso: str | None = None,
              estratificar: bool = False, populares: int = 0) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Relativos por código y región (y nacional por código) entre dos períodos.

    `agregador`: "media" (Jevons: media geométrica simple de los relativos, el método del índice),
    "recortada" (deja afuera el 5% más bajo y el 5% más alto de cada clase) o "mediana".
    `peso`: None (cada producto-cadena vale lo mismo) o "sucursales" (pesa por la cantidad de
    sucursales de la cadena que informan ese producto).
    `estratificar`: Jevons dentro de cada variedad (columna `variedad`, ver variedades.py) y media
    ponderada de las variedades con sus pesos (decisión D-12 de METODOLOGIA.md).
    `populares`: si es > 0, solo entran los códigos de barras que venden al menos esa cantidad de
    comercios distintos en la región en el período actual (aproxima "el artículo más vendido" que
    elige el encuestador del INDEC; decisión D-13).
    """
    lim = cfg["limpieza"]
    extra = [c for c in ("n_suc", "variedad") if c in actual.columns]
    cols_a = [*CLAVE, "codigo", pl.col("precio").alias("p1")] + extra
    act = actual.filter(pl.col("n_dias") >= min_a)
    if populares:
        act = act.filter(pl.col("id_comercio").n_unique().over("id_producto", "region") >= populares)
    pares = (
        previo.filter(pl.col("n_dias") >= min_p).select(*CLAVE, pl.col("precio").alias("p0"))
        .join(act.select(cols_a), on=CLAVE, how="inner")
        .with_columns((pl.col("p1") / pl.col("p0")).alias("rel"))
        .filter(pl.col("rel").is_between(lim["relativo_min"], lim["relativo_max"]))
        .with_columns(pl.col("rel").log().alias("logrel"))
    )
    pesar = peso == "sucursales" and "n_suc" in pares.columns
    estratificar = estratificar and "variedad" in pares.columns
    if estratificar:
        from .variedades import MIN_ARTICULOS, PESO_OTROS, pesos
        pv = pesos(cfg.dir_config)

    def agrupar(por: list[str], sufijo: str) -> pl.DataFrame:
        d = pares
        if agregador == "recortada":
            d = d.filter(pl.col("logrel").is_between(pl.col("logrel").quantile(0.05).over(por),
                                                     pl.col("logrel").quantile(0.95).over(por)))
        if agregador == "mediana":
            valor = pl.col("logrel").median()
        elif pesar:
            valor = (pl.col("logrel") * pl.col("n_suc")).sum() / pl.col("n_suc").sum()
        else:
            valor = pl.col("logrel").mean()
        if not estratificar:
            return d.group_by(por).agg(valor.alias(f"logrel_{sufijo}"), pl.len().alias(f"n_{sufijo}"))
        v = (d.group_by(*por, "variedad").agg(valor.alias("_lr"), pl.len().alias("_n"))
             .join(pv, on=["codigo", "variedad"], how="left")
             .with_columns(pl.col("peso_var").fill_null(PESO_OTROS)))
        # variedades con pocos artículos: no entran (si ninguna llega, la clase usa todos sus artículos)
        ok = v.filter(pl.col("_n") >= MIN_ARTICULOS)
        est = ok.group_by(por).agg(((pl.col("_lr") * pl.col("peso_var")).sum() / pl.col("peso_var").sum())
                                   .alias(f"logrel_{sufijo}"), pl.col("_n").sum().alias(f"n_{sufijo}"))
        todo = d.group_by(por).agg(valor.alias(f"logrel_{sufijo}"), pl.len().alias(f"n_{sufijo}"))
        return pl.concat([est, todo.join(est.select(por), on=por, how="anti")], how="vertical_relaxed")

    return agrupar(["codigo", "region"], "reg"), agrupar(["codigo"], "nac")


def _bases_directas(periodos: list[str], tramo: dict[str, int], frecuencia: str) -> dict[str, str]:
    """Período base de la comparación directa de cada período (decisión D-14): el último período de
    diciembre del año anterior dentro del tramo continuo (sin períodos faltantes) que termina en el
    período anterior; si ese tramo no tiene un diciembre previo, su primer período. Así la base nunca
    queda del otro lado de un hueco (por ejemplo, la semana suelta de abril de 2025)."""
    out = {}
    for i, p in enumerate(periodos):
        if i == 0:
            continue
        anio = int(_mes_del_periodo(p, frecuencia)[:4])
        cont = [periodos[i - 1]]
        j = i - 1
        while j > 0 and tramo[periodos[j - 1]] == tramo[p] and _siguiente(periodos[j - 1], frecuencia) == periodos[j]:
            cont.insert(0, periodos[j - 1])
            j -= 1
        dic = [q for q in cont if _mes_del_periodo(q, frecuencia) == f"{anio - 1}-12"]
        out[p] = dic[-1] if dic else cont[0]
    return out


def _mes_del_periodo(periodo: str, frecuencia: str) -> str:
    if frecuencia == "mensual":
        return periodo
    return (_inicio(periodo, frecuencia) + timedelta(days=3)).strftime("%Y-%m")  # mes del jueves


def niveles_indec(cfg: Config, codigos: list[str]) -> tuple[pl.DataFrame, list[str]]:
    """Índice del INDEC (base dic-2016 = 100) de la serie publicada más específica que contiene a
    cada código, por región y mes: clase, si no su grupo, si no su división. Con eso se actualizan
    los ponderadores de la canasta al último mes publicado (ponderador x precio relativo)."""
    from .indec import directorio
    from .validacion import leer_indec
    ind = leer_indec(directorio(cfg)).filter(pl.col("region").is_in(REGIONES))
    if ind.is_empty():
        return pl.DataFrame(schema={"codigo": pl.Utf8, "region": pl.Utf8, "mes": pl.Utf8, "nivel_indec": pl.Float64}), []
    publicados = set(ind["codigo"].unique().to_list())
    partes = []
    for c in codigos:
        x = resolver_codigo(c, publicados, min_niveles=1)
        if x is None:
            continue
        partes.append(ind.filter(pl.col("codigo") == x).select(
            pl.lit(c).alias("codigo"), "region", "mes", pl.col("indice").alias("nivel_indec")))
    if not partes:
        return pl.DataFrame(schema={"codigo": pl.Utf8, "region": pl.Utf8, "mes": pl.Utf8, "nivel_indec": pl.Float64}), []
    niv = pl.concat(partes)
    return niv, sorted(niv["mes"].unique().to_list())


def calcular(cfg: Config, frecuencia: str = "mensual", ponderacion: str | None = None, web: bool = False,
             extra: pl.DataFrame | None = None, solo_tramo_actual: bool = False, solo_tramo: int | None = None,
             excluir: tuple[str, ...] = (), precio: str | None = None, agregador: str = "media",
             peso: str | None = None, estratificar: bool | None = None, populares: int | None = None,
             directo: bool | None = None) -> dict[str, pl.DataFrame]:
    """Las opciones que siguen existen para la carrera de métodos (carrera.py): el índice publicado
    usa los valores por defecto.
    `excluir`: clases que se dejan afuera. `precio`: "lista" o "efectivo" (con promociones).
    `agregador` y `peso`: ver `relativos`. `solo_tramo`: calcula solo ese tramo de la serie.
    `web`: suma los precios de las tiendas en línea (paneles web_mensual / web_semanal).
    `extra`: variaciones de clases que no salen de precios relevados sino de fuentes públicas
    (tarifas por norma, combustibles de Energía, alquileres): columnas periodo, codigo, region,
    logrel. Solo se usan en las clases y períodos sin dato propio.
    `solo_tramo_actual`: calcula solo el último tramo de la serie (más rápido, para las variantes).
    `estratificar`, `populares`, `directo`: ver METODOLOGIA.md (D-12, D-13, D-14). Si no se dan,
    se toman de config.yaml (indice.estratificar, indice.populares, indice.directo).
      directo: la variación de cada período sale de comparar contra una base fija (diciembre del año
               anterior) y no de encadenar período a período: evita la deriva del encadenamiento.
    `ponderacion`:
      "indec"  (por defecto) los ponderadores de la canasta del INDEC se actualizan con los índices
               del INDEC hasta su último mes publicado; de ahí en adelante, con las variaciones
               propias. La variación de un agregado es la suma de las variaciones propias de cada
               clase por esos ponderadores.
      "propia" el método anterior: los ponderadores se actualizan solo con el nivel propio desde
               el comienzo del tramo (se sigue calculando para comparar).
    """
    ic = cfg["indice"]
    ponderacion = ponderacion or ic.get("ponderacion", "indec")
    # El índice semanal puede tener su propio método (indice.semanal en config.yaml, decisión D-37):
    # el método mejorado se probó solo contra el INDEC mensual.
    met = {**ic, **(ic.get("semanal") or {})} if frecuencia == "semanal" else ic
    estratificar = met.get("estratificar", False) if estratificar is None else estratificar
    populares = int(met.get("populares", 0) if populares is None else populares)
    directo = met.get("directo", False) if directo is None else directo
    dir_estr = cfg.dir_config if estratificar else None
    col_precio = "precio_lista" if (precio or ic["precio"]) == "lista" else "precio_efectivo"
    min_dias = ic["min_dias_mes"] if frecuencia == "mensual" else ic["min_dias_semana"]
    pond = ponderadores_clase(cfg)
    codigos = set(pond["codigo"].unique().to_list())
    nombres = dict(pond.select("codigo", "nombre").unique("codigo").iter_rows())
    w_region = ponderadores_region(cfg)

    rutas = sorted(cfg.ruta(f"panel_{frecuencia}").glob("*.parquet"))
    if (solo_tramo_actual or solo_tramo is not None) and len(rutas) > 2:
        t0 = tramos([r.stem for r in rutas], frecuencia, ic.get("max_meses_salto", 24))
        cual = max(t0.values()) if solo_tramo is None else solo_tramo
        rutas = [r for r in rutas if t0[r.stem] == cual]
    if len(rutas) < 2:
        raise ValueError(f"Se necesitan al menos dos períodos ({frecuencia}) consolidados")
    periodos = [r.stem for r in rutas]
    dias = sorted(cfg.ruta("panel_diario").glob("*.parquet"))
    ultimo_dia = date.fromisoformat(dias[-1].stem) if dias else None

    clases = set()
    for r in rutas:
        clases |= set(pl.read_parquet(r, columns=["clase"])["clase"].unique().to_list())
        if web and _ruta_web(r).exists():
            clases |= set(pl.read_parquet(_ruta_web(r), columns=["clase"])["clase"].drop_nulls().unique().to_list())
    clases.discard(SIN_CLASIFICAR)
    clases -= set(excluir)
    if extra is not None and not extra.is_empty():
        # las clases externas entran con su propio código (el más específico que tenga ponderador)
        clases |= {c for c in extra["codigo"].unique().to_list() if resolver_codigo(c, codigos) and c not in excluir}
    mapa = {c: resolver_codigo(c, codigos) for c in sorted(clases)}
    sin_pond = sorted(c for c, v in mapa.items() if v is None)
    if sin_pond:
        log.warning("Clases sin ponderador (quedan fuera del índice): %s", sin_pond)
    usados = sorted({v for v in mapa.values() if v})
    for c in usados:
        hijos = [d for d in usados if d != c and d.startswith(c + ".")]
        if hijos:
            log.warning("El código %s y sus subcódigos %s se usan a la vez: revisar el clasificador", c, hijos)

    # 1. relativos elementales período a período
    minp = ic["min_productos_clase"]
    tramo = tramos(periodos, frecuencia, ic.get("max_meses_salto", 24))
    grilla = pl.DataFrame({"codigo": usados}).join(pl.DataFrame({"region": REGIONES}), how="cross")
    filas = []
    bases = _bases_directas(periodos, tramo, frecuencia) if directo else {}
    paneles_base: dict[str, tuple[pl.DataFrame, tuple[int, int]]] = {}
    ruta_de = dict(zip(periodos, rutas))
    previo, info_p = _cargar(rutas[0], col_precio, mapa, web, dir_estr), _info(rutas[0])
    for ruta, per, per_prev in zip(rutas[1:], periodos[1:], periodos[:-1]):
        actual, info_a = _cargar(ruta, col_precio, mapa, web, dir_estr), _info(ruta)
        if tramo[per] != tramo[per_prev]:  # comienza un tramo nuevo: no se compara con el anterior
            filas.append(grilla.with_columns(
                pl.lit(per).alias("periodo"), pl.lit(0.0).alias("logrel"), pl.lit("inicio_tramo").alias("origen"),
                pl.lit(0, dtype=pl.UInt32).alias("n_reg"), pl.lit(_en_curso(per, frecuencia, ultimo_dia)).alias("parcial"),
            ).select("periodo", "codigo", "region", "logrel", "origen", "n_reg", "parcial"))
            previo, info_p = actual, info_a
            continue
        kw = dict(agregador=agregador, peso=peso, estratificar=estratificar, populares=populares)
        b = bases.get(per)
        if directo and b and b != per_prev:
            # variación = (base -> actual) - (base -> anterior), con los mismos filtros
            if b not in paneles_base:
                paneles_base.clear()
                paneles_base[b] = (_cargar(ruta_de[b], col_precio, mapa, web, dir_estr), _info(ruta_de[b]))
            pb, info_b = paneles_base[b]
            mb = _min_dias(info_b[0], min_dias)
            r1, n1 = relativos(pb, actual, mb, _min_dias(info_a[0], min_dias), cfg, **kw)
            r0, n0 = relativos(pb, previo, mb, _min_dias(info_p[0], min_dias), cfg, **kw)
            reg = r1.join(r0.select("codigo", "region", pl.col("logrel_reg").alias("_r0")), on=["codigo", "region"]).with_columns(
                (pl.col("logrel_reg") - pl.col("_r0")).alias("logrel_reg")).drop("_r0")
            nac = n1.join(n0.select("codigo", pl.col("logrel_nac").alias("_n0")), on="codigo").with_columns(
                (pl.col("logrel_nac") - pl.col("_n0")).alias("logrel_nac")).drop("_n0")
        else:
            reg, nac = relativos(previo, actual, _min_dias(info_p[0], min_dias), _min_dias(info_a[0], min_dias), cfg, **kw)
        r = (
            grilla.join(reg, on=["codigo", "region"], how="left")
            .join(nac, on="codigo", how="left")
            .with_columns(
                pl.when(pl.col("n_reg") >= minp).then(pl.col("logrel_reg"))
                .when(pl.col("n_nac") >= minp).then(pl.col("logrel_nac"))
                .otherwise(None).alias("logrel"),  # sin datos: no se supone que el precio no cambió
                pl.when(pl.col("n_reg") >= minp).then(pl.lit("regional"))
                .when(pl.col("n_nac") >= minp).then(pl.lit("nacional"))
                .otherwise(pl.lit("sin_datos")).alias("origen"),
                pl.col("n_reg").fill_null(0).cast(pl.UInt32),
                pl.lit(per).alias("periodo"),
                pl.lit(_en_curso(per, frecuencia, ultimo_dia)).alias("parcial"),
            )
        )
        filas.append(r.select("periodo", "codigo", "region", "logrel", "origen", "n_reg", "parcial"))
        previo, info_p = actual, info_a

    info0 = _info(rutas[0])
    base0 = grilla.with_columns(
        pl.lit(periodos[0]).alias("periodo"), pl.lit(0.0).alias("logrel"), pl.lit("base").alias("origen"),
        pl.lit(0, dtype=pl.UInt32).alias("n_reg"), pl.lit(_en_curso(periodos[0], frecuencia, ultimo_dia)).alias("parcial"),
    ).select("periodo", "codigo", "region", "logrel", "origen", "n_reg", "parcial")
    todas = pl.concat([base0, *filas])
    if extra is not None and not extra.is_empty():
        ext = (extra.with_columns(pl.col("codigo").replace_strict(mapa, default=None, return_dtype=pl.Utf8))
               .filter(pl.col("codigo").is_not_null(), pl.col("logrel").is_not_null())
               .group_by("periodo", "codigo", "region").agg(pl.col("logrel").mean().alias("_lr_ext")))
        todas = todas.join(ext, on=["periodo", "codigo", "region"], how="left").with_columns(
            pl.when(pl.col("logrel").is_null() & pl.col("_lr_ext").is_not_null() & (pl.col("origen") == "sin_datos"))
            .then(pl.lit("externo")).otherwise(pl.col("origen")).alias("origen"),
            pl.when(pl.col("origen") == "sin_datos").then(pl.coalesce("logrel", "_lr_ext")).otherwise(pl.col("logrel"))
            .alias("logrel")).drop("_lr_ext")
    elem = (
        todas
        .with_columns(pl.col("periodo").replace_strict(tramo, return_dtype=pl.Int32).alias("tramo"))
        .sort("codigo", "region", "periodo")
        .with_columns(pl.col("logrel").fill_null(0.0).cum_sum().over("codigo", "region", "tramo").alias("_acum"))
        .with_columns(
            # nivel de la clase: vacío en los períodos sin datos (no se arrastra el anterior)
            pl.when(pl.col("logrel").is_null()).then(None).otherwise(pl.col("_acum").exp() * 100).alias("indice"),
            # nivel previo, solo para ponderar el eslabón siguiente (no entra como variación de precio)
            pl.col("_acum").shift(1).over("codigo", "region", "tramo").fill_null(0.0).exp().alias("_nivel_prev"))
    )
    ult_indec = None
    if ponderacion == "indec":
        niv, meses_indec = niveles_indec(cfg, usados)
        if not meses_indec:
            log.warning("Sin series del INDEC: se usan los ponderadores con el nivel propio")
            ponderacion = "propia"
    if ponderacion == "indec":
        ult_indec = meses_indec[-1]
        mes_p = {p: _mes_del_periodo(p, frecuencia) for p in periodos}
        # mes de referencia: último mes del INDEC anterior al mes del período
        ref = {p: max((m for m in meses_indec if m < mes_p[p]), default=None) for p in periodos}
        # último período propio (del mismo tramo) que no es posterior al mes de referencia
        p_ref = {}
        for i, p in enumerate(periodos):
            cands = [q for q in periodos[:i] if ref[p] is not None and mes_p[q] <= ref[p] and tramo[q] == tramo[p]]
            p_ref[p] = cands[-1] if cands else None
        mapa_ref = pl.DataFrame({"periodo": periodos, "_mes_ref": [ref[p] for p in periodos],
                                 "_p_ref": [p_ref[p] for p in periodos]},
                                schema={"periodo": pl.Utf8, "_mes_ref": pl.Utf8, "_p_ref": pl.Utf8})
        elem = (
            elem.join(mapa_ref, on="periodo", how="left")
            .join(elem.select("codigo", "region", pl.col("periodo").alias("_p_ref"), pl.col("_acum").alias("_acum_ref")),
                  on=["codigo", "region", "_p_ref"], how="left")
            .join(niv.rename({"mes": "_mes_ref"}), on=["codigo", "region", "_mes_ref"], how="left")
            .with_columns(pl.col("_acum").shift(1).over("codigo", "region", "tramo").fill_null(0.0).alias("_acum_prev"))
            # ponderador actualizado = ponderador base x índice INDEC del mes de referencia x variación
            # propia acumulada desde entonces (solo meses que el INDEC todavía no publicó)
            .with_columns(((pl.col("nivel_indec").fill_null(100.0) / 100)
                           * (pl.col("_acum_prev") - pl.col("_acum_ref").fill_null(0.0)).exp()).alias("_nivel_prev"))
            .drop("_mes_ref", "_p_ref", "_acum_ref", "_acum_prev", "nivel_indec")
        )
    elem = elem.drop("_acum")
    # base = 100: el mes_base configurado en su tramo; en los demás, el primer período del tramo
    base = ic.get("mes_base") if frecuencia == "mensual" else None
    if base and base not in periodos:
        raise ValueError(f"Período base {base} no está entre los períodos con datos")
    if base:
        elem = elem.with_columns(
            pl.when(pl.col("tramo") == tramo[base])
            .then(pl.col("indice") / pl.col("indice").filter(pl.col("periodo") == base).first().over("codigo", "region", "tramo") * 100)
            .otherwise(pl.col("indice")).alias("indice"))

    # 2. agregación con ponderadores de la canasta regional
    elem = elem.join(pond.select("codigo", "region", "ponderacion"), on=["codigo", "region"], how="left").with_columns(
        pl.col("ponderacion").fill_null(0.0), pl.col("codigo").str.slice(0, 2).alias("division"),
        pl.col("codigo").str.slice(0, 4).alias("grupo"))
    parcial = elem.group_by("periodo").agg(pl.col("parcial").any(), pl.col("tramo").first())

    def agregar(df: pl.DataFrame, por: list[str]) -> pl.DataFrame:
        """Laspeyres encadenado: en cada período, promedio de los relativos de las clases CON datos,
        ponderados por su peso en la canasta y su nivel del período anterior. Una clase sin datos en
        un período queda afuera de ese eslabón: no se le supone variación cero ni se arrastra nada."""
        w = pl.col("ponderacion") * pl.col("_nivel_prev")
        con = pl.col("logrel").is_not_null()
        eslabon = df.group_by(*por, "tramo").agg(
            ((w * pl.col("logrel").exp()).filter(con).sum() / w.filter(con).sum()).alias("_rel"),
            w.filter(con).sum().alias("_w"),
            pl.col("ponderacion").sum().alias("ponderacion"),
            pl.col("n_reg").sum().alias("n_productos"),
        )
        claves = [c for c in por if c != "periodo"]
        eslabon = eslabon.sort(*claves, "periodo").with_columns(
            (pl.col("_rel").log().fill_null(0.0).cum_sum().over(*claves, "tramo").exp() * 100).alias("indice"))
        # sin ninguna clase con datos en el período: el agregado queda vacío ese período
        return eslabon.with_columns(pl.when(pl.col("_rel").is_null()).then(None).otherwise(pl.col("indice"))
                                    .alias("indice")).drop("tramo")

    regional = pl.concat([
        agregar(elem, ["periodo", "region", "codigo"]).with_columns(pl.lit("clase").alias("nivel")),
        agregar(elem, ["periodo", "region", "division"]).rename({"division": "codigo"})
        .with_columns(pl.lit("division").alias("nivel")),
        agregar(elem, ["periodo", "region", "grupo"]).rename({"grupo": "codigo"})
        .with_columns(pl.lit("grupo").alias("nivel")),
        agregar(elem, ["periodo", "region"]).with_columns(pl.lit("TOTAL").alias("codigo"), pl.lit("total").alias("nivel")),
    ], how="diagonal")

    eslabones = regional.select("periodo", "region", "nivel", "codigo", "_rel", "_w", "ponderacion", "n_productos")
    regional = regional.drop("_rel", "_w")
    if base:  # el agregado encadenado arranca en 100 al inicio del tramo: se lleva a 100 en el mes base
        regional = regional.join(parcial.select("periodo", "tramo"), on="periodo").with_columns(
            pl.when(pl.col("tramo") == tramo[base])
            .then(pl.col("indice") / pl.col("indice").filter(pl.col("periodo") == base).first()
                  .over("region", "nivel", "codigo", "tramo") * 100)
            .otherwise(pl.col("indice")).alias("indice")).drop("tramo")

    # 3. nacional
    if ponderacion == "indec":
        # variación nacional = suma de las variaciones regionales por el ponderador actualizado de
        # cada región (peso de la región x canasta cubierta a precios del mes de referencia)
        en = (
            eslabones.with_columns(pl.col("region").replace_strict(w_region, return_dtype=pl.Float64).alias("w_region"))
            .with_columns((pl.col("w_region") * pl.col("_w")).alias("_ww"))
            .group_by("periodo", "codigo", "nivel")
            .agg(pl.when((pl.col("_rel").null_count() == 0) & (pl.len() == len(REGIONES)))  # si falta una región, vacío
                 .then((pl.col("_rel") * pl.col("_ww")).sum() / pl.col("_ww").sum()).otherwise(None).alias("_rel"),
                 (pl.col("w_region") * pl.col("ponderacion")).sum().alias("ponderacion"), pl.col("n_productos").sum())
            .join(parcial.select("periodo", "tramo"), on="periodo")
            .sort("nivel", "codigo", "periodo")
        )
        nac = en.with_columns(
            (pl.col("_rel").log().fill_null(0.0).cum_sum().over("nivel", "codigo", "tramo").exp() * 100).alias("indice")
        ).with_columns(pl.when(pl.col("_rel").is_null()).then(None).otherwise(pl.col("indice")).alias("indice"),
                       pl.lit(NACIONAL).alias("region")).drop("_rel", "tramo")
        if base:
            nac = nac.join(parcial.select("periodo", "tramo"), on="periodo").with_columns(
                pl.when(pl.col("tramo") == tramo[base])
                .then(pl.col("indice") / pl.col("indice").filter(pl.col("periodo") == base).first()
                      .over("nivel", "codigo", "tramo") * 100)
                .otherwise(pl.col("indice")).alias("indice")).drop("tramo")
    else:
        nac = (
            regional.with_columns(pl.col("region").replace_strict(w_region, return_dtype=pl.Float64).alias("w_region"))
            .with_columns((pl.col("w_region") * pl.col("ponderacion")).alias("w"))
            .group_by("periodo", "codigo", "nivel")
            .agg(pl.when(pl.col("indice").null_count() == 0)  # si falta una región, el nacional queda vacío
                 .then((pl.col("indice") * pl.col("w")).sum() / pl.col("w").sum()).otherwise(None).alias("indice"),
                 pl.col("w").sum().alias("ponderacion"), pl.col("n_productos").sum())
            .with_columns(pl.lit(NACIONAL).alias("region"))
        )

    nombres_div = dict(pl.read_csv(cfg.dir_config / "ponderadores_division.csv", schema_overrides={"codigo": pl.Utf8})
                       .select("codigo", "nombre").iter_rows())
    todos = (
        pl.concat([regional, nac.select(regional.columns)])
        .join(parcial, on="periodo", how="left")
        .sort("region", "nivel", "codigo", "periodo")
        .with_columns(
            pl.col("periodo").shift(1).over("region", "nivel", "codigo", "tramo").alias("periodo_anterior"),
            ((pl.col("indice") / pl.col("indice").shift(1).over("region", "nivel", "codigo", "tramo") - 1) * 100)
            .alias("var_pct"),
            (pl.col("periodo").str.slice(0, 4).cast(pl.Int32) - 1).cast(pl.Utf8)
            .add(pl.col("periodo").str.slice(4)).alias("_periodo_ia"),
            pl.when(pl.col("nivel") == "total").then(pl.lit("Bienes relevados (total)"))
            .when(pl.col("nivel") == "division").then(pl.col("codigo").replace_strict(nombres_div, default=""))
            .when(pl.col("nivel") == "grupo").then(pl.col("codigo").replace_strict(nombres, default=""))
            .otherwise(pl.col("codigo").replace_strict(nombres, default="")).alias("nombre"),
        )
        .join(
            pl.concat([regional, nac.select(regional.columns)]).join(parcial.select("periodo", "tramo"), on="periodo")
            .select("region", "nivel", "codigo", "tramo", pl.col("periodo").alias("_periodo_ia"),
                    pl.col("indice").alias("_indice_ia")),
            on=["region", "nivel", "codigo", "tramo", "_periodo_ia"], how="left",
        )
        .with_columns(((pl.col("indice") / pl.col("_indice_ia") - 1) * 100).alias("var_interanual_pct"))
        .with_columns(
            # salto: el período anterior con datos no es el inmediato anterior (ej. la copia de la
            # Wayback Machine de abril de 2025). La variación de ese tramo no es mensual ni semanal.
            (pl.col("periodo_anterior").is_not_null()
             & (pl.col("periodo_anterior").map_elements(lambda p: _siguiente(p, frecuencia), return_dtype=pl.Utf8)
                != pl.col("periodo"))).alias("salto"),
        )
        .with_columns(
            pl.when(pl.col("salto")).then(pl.col("var_pct")).otherwise(None).alias("var_salto_pct"),
            pl.when(pl.col("salto")).then(None).otherwise(pl.col("var_pct")).alias("var_pct"),
        )
        .select("periodo", "region", "nivel", "codigo", "nombre", "indice", "var_pct",
                "var_interanual_pct", "n_productos", "parcial", "salto", "periodo_anterior", "var_salto_pct", "tramo")
        .with_columns(pl.col("indice", "var_pct", "var_interanual_pct", "var_salto_pct").round(4))
    )

    ult = elem.filter(pl.col("periodo") == periodos[-1])
    cob = ult.group_by("region").agg(pl.col("ponderacion").sum().alias("ponderacion_cubierta_pct")).with_columns(
        pl.col("region").replace_strict(w_region, return_dtype=pl.Float64).alias("w"))
    cobertura = pl.concat([
        cob.select("region", "ponderacion_cubierta_pct"),
        cob.select(pl.lit(NACIONAL).alias("region"),
                   ((pl.col("ponderacion_cubierta_pct") * pl.col("w")).sum() / pl.col("w").sum())
                   .alias("ponderacion_cubierta_pct")),
    ]).with_columns(pl.col("ponderacion_cubierta_pct").round(2))
    mapeo = pl.DataFrame({"clase": list(mapa), "codigo_ponderador": list(mapa.values())},
                         schema={"clase": pl.Utf8, "codigo_ponderador": pl.Utf8})
    calidad = elem.group_by("periodo", "origen").agg(pl.len().alias("n_agregados")).sort("periodo", "origen")

    return {"indices": todos.with_columns(pl.lit(ponderacion).alias("ponderacion_metodo")), "cobertura": cobertura,
            "mapeo_clases": mapeo, "calidad": calidad}


def guardar(res: dict[str, pl.DataFrame], cfg: Config, frecuencia: str) -> list[Path]:
    dir_res = cfg.ruta("resultados")
    rutas = []
    for nombre, df in res.items():
        p = dir_res / f"{nombre}_{frecuencia}.csv"
        df.write_csv(p)
        rutas.append(p)
    return rutas
