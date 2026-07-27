from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .almacen import Almacen

log = logging.getLogger("faro.emision")

MOTIVO_OK = "OK"


def identificar_ejecucion(manifest: dict[str, Any]) -> str:
    semilla = "|".join(
        str(manifest.get(c, ""))
        for c in ("dataset", "version", "publicado_utc", "sha256")
    )
    return hashlib.sha256(semilla.encode("utf-8")).hexdigest()[:12]


def _fecha_de(manifest: dict[str, Any]) -> pd.Timestamp:
    crudo = manifest.get("publicado_utc") or datetime.now(timezone.utc).isoformat()
    return pd.to_datetime(crudo, utc=True, errors="coerce").tz_localize(None)


def filas_ejecucion(manifest: dict[str, Any]) -> pd.DataFrame:
    ts = _fecha_de(manifest)
    ejecucion = manifest.get("ejecucion", {}) or {}
    return pd.DataFrame(
        [
            {
                "ejecucion_id": identificar_ejecucion(manifest),
                "fecha": ts.date(),
                "publicado_utc": ts,
                "version_dataset": manifest.get("version"),
                "contrato": manifest.get("contrato"),
                "entorno": ejecucion.get("entorno"),
                "git_sha": ejecucion.get("git_sha"),
                "sha256": manifest.get("sha256"),
                "filas": int(manifest.get("filas", 0)),
            }
        ]
    )


def filas_metricas(manifest: dict[str, Any]) -> pd.DataFrame:
    ejec_id = identificar_ejecucion(manifest)
    fecha = _fecha_de(manifest).date()
    metricas = manifest.get("metricas", {}) or {}
    return pd.DataFrame(
        [
            {"ejecucion_id": ejec_id, "fecha": fecha, "metrica": k, "valor": float(v)}
            for k, v in metricas.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
    )


def filas_rechazos(silver: pd.DataFrame, manifest: dict[str, Any]) -> pd.DataFrame:
    if silver is None or silver.empty:
        return pd.DataFrame()

    columnas = {"_fuente", "motivo_no_contacto", "cliente_id"}
    if not columnas.issubset(silver.columns):
        raise ValueError(f"silver debe contener {sorted(columnas)}")

    agregado = (
        silver.groupby(["_fuente", "motivo_no_contacto"], dropna=False)
        .agg(registros=("cliente_id", "size"), clientes=("cliente_id", "nunique"))
        .reset_index()
        .rename(columns={"_fuente": "fuente", "motivo_no_contacto": "motivo"})
    )
    agregado["ejecucion_id"] = identificar_ejecucion(manifest)
    agregado["fecha"] = _fecha_de(manifest).date()
    return agregado


def filas_detalle(silver: pd.DataFrame, manifest: dict[str, Any]) -> pd.DataFrame:
    if silver is None or silver.empty:
        return pd.DataFrame()

    rechazados = silver[silver["motivo_no_contacto"] != MOTIVO_OK].copy()
    if rechazados.empty:
        return pd.DataFrame()

    salida = pd.DataFrame(
        {
            "ejecucion_id": identificar_ejecucion(manifest),
            "fecha": _fecha_de(manifest).date(),
            "cliente_id": rechazados["cliente_id"].astype(str),
            "telefono_hash": rechazados.get("telefono_hash"),
            "telefono_enmascarado": rechazados.get("telefono_enmascarado"),
            "fuente": rechazados["_fuente"],
            "motivo": rechazados["motivo_no_contacto"],
        }
    )


    sin_hash = salida["telefono_hash"].isna()
    if sin_hash.any():
        crudo = rechazados.loc[sin_hash.to_numpy(), "telefono_crudo"].astype(str)
        salida.loc[sin_hash, "telefono_hash"] = [
            "crudo:" + hashlib.sha256(v.encode("utf-8")).hexdigest()[:24] for v in crudo
        ]
    return salida.drop_duplicates(subset=["ejecucion_id", "telefono_hash", "motivo"])


def registrar(
    almacen: Almacen,
    manifest: dict[str, Any],
    silver: pd.DataFrame | None = None,
    con_detalle: bool = True,
) -> dict[str, int]:
    escritas = {
        "ejecuciones": almacen.anexar("ejecuciones", filas_ejecucion(manifest)),
        "metricas": almacen.anexar("metricas", filas_metricas(manifest)),
    }
    if silver is not None and not silver.empty:
        escritas["rechazos"] = almacen.anexar("rechazos", filas_rechazos(silver, manifest))
        if con_detalle:
            escritas["rechazos_detalle"] = almacen.anexar(
                "rechazos_detalle", filas_detalle(silver, manifest)
            )
    log.info("Corrida %s registrada: %s", identificar_ejecucion(manifest), escritas)
    return escritas


def backfill(almacen: Almacen, raiz_datasets: str | Path) -> pd.DataFrame:
    manifests = sorted(Path(raiz_datasets).rglob("manifest.json"))
    resultados = []
    for ruta in manifests:
        try:
            manifest = json.loads(ruta.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("Manifest ilegible, se omite: %s", ruta)
            continue
        escritas = registrar(almacen, manifest, silver=None)
        resultados.append(
            {
                "manifest": str(ruta),
                "ejecucion_id": identificar_ejecucion(manifest),
                "version": manifest.get("version"),
                "metricas": escritas.get("metricas", 0),
            }
        )
    log.info("Backfill: %d manifests procesados", len(resultados))
    return pd.DataFrame(resultados)
