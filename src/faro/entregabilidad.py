from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .almacen import Almacen

log = logging.getLogger("faro.entregabilidad")


EVENTOS_POSITIVOS = ("otp_confirmado", "entrega_exitosa", "llamada_contestada")

EVENTOS_FATALES = ("rebote_duro", "numero_inexistente", "numero_dado_de_baja")

EVENTOS_DUDOSOS = ("rebote_blando", "numero_errado", "no_corresponde_al_titular")

EVENTOS_VALIDOS = EVENTOS_POSITIVOS + EVENTOS_FATALES + EVENTOS_DUDOSOS

ESTADOS = ("verificado", "presunto", "sospechoso", "inactivo")


def _sql_lista(valores: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in valores)


def identificar_evento(fila: pd.Series) -> str:
    semilla = "|".join(
        str(fila.get(c, "")) for c in ("telefono_hash", "evento", "canal", "ocurrido_utc")
    )
    return hashlib.sha256(semilla.encode("utf-8")).hexdigest()[:16]


def ingerir(almacen: Almacen, eventos: pd.DataFrame) -> int:
    if eventos is None or eventos.empty:
        return 0

    requeridas = {"telefono_hash", "evento", "ocurrido_utc"}
    faltantes = requeridas - set(eventos.columns)
    if faltantes:
        raise ValueError(f"Faltan columnas en los eventos: {sorted(faltantes)}")

    df = eventos.copy()
    desconocidos = set(df["evento"].unique()) - set(EVENTOS_VALIDOS)
    if desconocidos:
        raise ValueError(
            f"Eventos no reconocidos: {sorted(desconocidos)}. "
            f"Válidos: {sorted(EVENTOS_VALIDOS)}"
        )

    df["ocurrido_utc"] = pd.to_datetime(df["ocurrido_utc"], errors="coerce")
    if df["ocurrido_utc"].isna().any():
        raise ValueError("Hay eventos con 'ocurrido_utc' ilegible")
    if "canal" not in df.columns:
        df["canal"] = "desconocido"
    df["evento_id"] = df.apply(identificar_evento, axis=1)

    return almacen.anexar("entregabilidad", df)


def calcular_estados(
    almacen: Almacen,
    ventana_dias: int = 365,
    dudosos_para_sospechoso: int = 2,
    ahora: datetime | None = None,
) -> pd.DataFrame:
    ahora = ahora or datetime.now(timezone.utc).replace(tzinfo=None)
    desde = (ahora - timedelta(days=ventana_dias)).isoformat(sep=" ", timespec="seconds")

    return almacen.sql(
        f"""
        WITH ventana AS (
            SELECT * FROM v_entregabilidad
            WHERE ocurrido_utc >= CAST($desde AS TIMESTAMP)
        ),
        agregado AS (
            SELECT
                telefono_hash,
                max(CASE WHEN evento IN ({_sql_lista(EVENTOS_POSITIVOS)})
                         THEN ocurrido_utc END) AS ultimo_positivo,
                max(CASE WHEN evento IN ({_sql_lista(EVENTOS_FATALES)})
                         THEN ocurrido_utc END) AS ultimo_fatal,
                count(*)                        AS eventos_totales
            FROM ventana
            GROUP BY 1
        ),
        dudosos AS (
            SELECT v.telefono_hash, count(*) AS eventos_dudosos
            FROM ventana v
            JOIN agregado a USING (telefono_hash)
            WHERE v.evento IN ({_sql_lista(EVENTOS_DUDOSOS)})
              AND (a.ultimo_positivo IS NULL OR v.ocurrido_utc > a.ultimo_positivo)
            GROUP BY 1
        )
        SELECT
            a.telefono_hash,
            CASE
                WHEN a.ultimo_fatal IS NOT NULL
                     AND (a.ultimo_positivo IS NULL OR a.ultimo_fatal > a.ultimo_positivo)
                    THEN 'inactivo'
                WHEN coalesce(d.eventos_dudosos, 0) >= CAST($limite AS BIGINT)
                    THEN 'sospechoso'
                WHEN a.ultimo_positivo IS NOT NULL
                    THEN 'verificado'
                ELSE 'presunto'
            END                                   AS estado_verificado,
            a.ultimo_positivo,
            a.ultimo_fatal,
            coalesce(d.eventos_dudosos, 0)        AS eventos_dudosos,
            a.eventos_totales
        FROM agregado a
        LEFT JOIN dudosos d USING (telefono_hash)
        """,
        {"desde": desde, "limite": int(dudosos_para_sospechoso)},
    )


def exportar_estados(
    almacen: Almacen,
    destino: str | Path,
    ventana_dias: int = 365,
    dudosos_para_sospechoso: int = 2,
) -> Path:
    estados = calcular_estados(almacen, ventana_dias, dudosos_para_sospechoso)
    ruta = Path(destino)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    columnas = ["telefono_hash", "estado_verificado", "eventos_dudosos", "eventos_totales"]
    faltan = [c for c in columnas if c not in estados.columns]
    for c in faltan:
        estados[c] = pd.NA
    estados[columnas].to_parquet(ruta, index=False, compression="snappy")
    log.info("Estados exportados a %s (%d teléfonos)", ruta, len(estados))
    return ruta


def resumen(almacen: Almacen, **kwargs) -> pd.DataFrame:
    estados = calcular_estados(almacen, **kwargs)
    if estados.empty:
        return pd.DataFrame(columns=["estado_verificado", "telefonos", "porcentaje"])
    conteo = (
        estados["estado_verificado"].value_counts().rename_axis("estado_verificado").reset_index(name="telefonos")
    )
    conteo["porcentaje"] = (conteo["telefonos"] / conteo["telefonos"].sum()).round(4)
    return conteo
