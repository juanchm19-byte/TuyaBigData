from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from . import entregabilidad as mod_entrega
from . import ordenes as mod_ordenes
from . import reporte as mod_reporte
from .almacen import Almacen
from .emision import backfill
from .kpi import calcular, cargar_catalogo, marcador_fuentes


def _contexto(args: argparse.Namespace):
    config = mod_ordenes.cargar_config(args.config)
    obs = config.get("observatorio", {}) or {}
    almacen = Almacen(args.ruta or obs.get("ruta", "data/observatorio"))
    catalogo = cargar_catalogo(args.catalogo or obs.get("catalogo", "conf/catalogo_kpi.yml"))
    return almacen, catalogo, config


def _imprimir(df: pd.DataFrame, formato: str) -> None:
    if df.empty:
        print("(sin resultados)")
        return
    if formato == "json":
        print(
            df.astype(object).where(pd.notna(df), None).to_json(
                orient="records", force_ascii=False, indent=2, date_format="iso"
            )
        )
    elif formato == "csv":
        print(df.to_csv(index=False))
    else:
        print(df.to_string(index=False))


def main(argv: list[str] | None = None) -> int:


    comunes = argparse.ArgumentParser(add_help=False)
    comunes.add_argument("--ruta", help="raíz del almacén del observatorio")
    comunes.add_argument("--config", default="conf/faro.yml")
    comunes.add_argument("--catalogo", help="catálogo de KPI")
    comunes.add_argument("--formato", choices=["tabla", "json", "csv"], default="tabla")
    comunes.add_argument("--verboso", action="store_true")

    p = argparse.ArgumentParser(prog="faro", description=__doc__, parents=[comunes],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="comando", required=True)

    sub.add_parser("tablero", parents=[comunes], help="KPI de la última corrida")
    sub.add_parser("fuentes", parents=[comunes], help="marcador de calidad por fuente")
    sub.add_parser("resumen", parents=[comunes], help="estado del almacén")
    sub.add_parser("purgar", parents=[comunes], help="aplicar la retención declarada")

    s = sub.add_parser("backfill", parents=[comunes], help="cargar el histórico desde los manifests")
    s.add_argument("--datasets", default="data/gold")

    s = sub.add_parser("ordenes", parents=[comunes], help="ciclo de órdenes de corrección")
    s.add_argument("accion", choices=["detectar", "listar", "verificar", "ciclo"])
    s.add_argument("--todas", action="store_true", help="incluir las cerradas")

    s = sub.add_parser("entregabilidad", parents=[comunes], help="retroalimentación de contacto real")
    s.add_argument("accion", choices=["ingerir", "estados", "exportar", "resumen"])
    s.add_argument("--eventos", help="CSV/parquet con los eventos")
    s.add_argument("--destino", default="data/observatorio/estados_verificados.parquet")

    s = sub.add_parser("digest", parents=[comunes], help="reporte dirigido")
    s.add_argument("--fuente", help="digest de una fuente; si se omite, el general")
    s.add_argument("--salida", help="archivo markdown donde escribirlo")

    s = sub.add_parser("exportar", parents=[comunes], help="materializar tablas para la herramienta de BI")
    s.add_argument("--destino", default="build/faro")

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verboso else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    almacen, catalogo, config = _contexto(args)
    obs = config.get("observatorio", {}) or {}
    hoy = date.today()

    if args.comando == "tablero":
        kpis = calcular(almacen, catalogo, dias_tendencia=int(obs.get("dias_tendencia", 28)))
        if args.formato == "tabla":
            print(mod_reporte.tablero_texto(kpis))
        else:
            _imprimir(kpis, args.formato)

    elif args.comando == "fuentes":
        _imprimir(marcador_fuentes(almacen), args.formato)

    elif args.comando == "resumen":
        _imprimir(almacen.resumen(), args.formato)

    elif args.comando == "purgar":
        print(json.dumps(almacen.purgar(hoy), indent=2, ensure_ascii=False))

    elif args.comando == "backfill":
        _imprimir(backfill(almacen, args.datasets), args.formato)

    elif args.comando == "ordenes":
        if args.accion in ("detectar", "ciclo"):
            nuevas = mod_ordenes.detectar(almacen, config, catalogo, hoy=hoy)
            print(f"Órdenes nuevas: {len(nuevas)}")
        if args.accion in ("verificar", "ciclo"):
            cerradas = mod_ordenes.verificar_cierre(almacen, config, catalogo, hoy=hoy)
            print(f"Órdenes cerradas automáticamente: {len(cerradas)}")
        estado = None if args.todas else mod_ordenes.ABIERTA
        _imprimir(mod_ordenes.listar(almacen, estado, hoy), args.formato)

    elif args.comando == "entregabilidad":
        conf_e = config.get("entregabilidad", {}) or {}
        ventana = int(conf_e.get("ventana_dias", 365))
        limite = int(conf_e.get("dudosos_para_sospechoso", 2))
        if args.accion == "ingerir":
            if not args.eventos:
                p.error("entregabilidad ingerir requiere --eventos")
            ruta = Path(args.eventos)
            eventos = pd.read_parquet(ruta) if ruta.suffix == ".parquet" else pd.read_csv(ruta)
            print(f"Eventos ingeridos: {mod_entrega.ingerir(almacen, eventos)}")
        elif args.accion == "estados":
            _imprimir(mod_entrega.calcular_estados(almacen, ventana, limite), args.formato)
        elif args.accion == "resumen":
            _imprimir(mod_entrega.resumen(almacen, ventana_dias=ventana,
                                          dudosos_para_sospechoso=limite), args.formato)
        else:
            destino = mod_entrega.exportar_estados(almacen, args.destino, ventana, limite)
            print(f"Estados exportados a {destino}")
            print("El pipeline los leerá en la siguiente corrida para puntuar el golden record.")

    elif args.comando == "digest":
        if args.fuente:
            texto = mod_reporte.digest_fuente(almacen, args.fuente, config, hoy)
        else:
            kpis = calcular(almacen, catalogo, dias_tendencia=int(obs.get("dias_tendencia", 28)))
            texto = mod_reporte.digest_general(kpis, mod_ordenes.listar(almacen, hoy=hoy), hoy)
        if args.salida:
            Path(args.salida).parent.mkdir(parents=True, exist_ok=True)
            Path(args.salida).write_text(texto, encoding="utf-8")
            print(f"Digest escrito en {args.salida}")
        else:
            print(texto)

    elif args.comando == "exportar":
        destino = Path(args.destino)
        destino.mkdir(parents=True, exist_ok=True)
        kpis = calcular(almacen, catalogo, dias_tendencia=int(obs.get("dias_tendencia", 28)))
        kpis.to_parquet(destino / "kpis.parquet", index=False)
        marcador_fuentes(almacen).to_parquet(destino / "fuentes.parquet", index=False)
        almacen.leer("metricas").to_parquet(destino / "metricas.parquet", index=False)
        mod_ordenes.listar(almacen, None, hoy).to_parquet(destino / "ordenes.parquet", index=False)
        print(f"Tablas materializadas en {destino}/ (kpis, fuentes, metricas, ordenes)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
