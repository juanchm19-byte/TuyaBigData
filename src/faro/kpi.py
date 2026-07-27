from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .almacen import Almacen

log = logging.getLogger("faro.kpi")

DIRECCIONES = {"mayor_mejor", "menor_mejor"}


def _ahora_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class DefinicionKPI:
    id: str
    nombre: str
    pregunta: str
    bloque: str
    unidad: str
    direccion: str
    dueno: str
    meta: float | None = None
    metrica: str | None = None
    expresion: str | None = None
    parametros: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.direccion not in DIRECCIONES:
            raise ValueError(f"{self.id}: dirección inválida '{self.direccion}'")
        if not self.metrica and not self.expresion:
            raise ValueError(f"{self.id}: debe declarar 'metrica' o 'expresion'")
        if self.metrica and self.expresion:
            raise ValueError(f"{self.id}: declara 'metrica' y 'expresion'; elija una")

    @property
    def sql(self) -> str:
        if self.expresion:
            return self.expresion
        return (
            "SELECT valor FROM v_metricas "
            f"WHERE ejecucion_id = $ejec AND metrica = '{self.metrica}'"
        )


def cargar_catalogo(ruta: str | Path) -> list[DefinicionKPI]:
    datos = yaml.safe_load(Path(ruta).read_text(encoding="utf-8")) or {}
    definiciones = [DefinicionKPI(**d) for d in datos.get("kpis", [])]
    ids = [d.id for d in definiciones]
    repetidos = {i for i in ids if ids.count(i) > 1}
    if repetidos:
        raise ValueError(f"Identificadores de KPI repetidos: {sorted(repetidos)}")
    log.info("Catálogo cargado: %d KPI", len(definiciones))
    return definiciones


_RE_PARAM = re.compile(r"\$(\w+)")


def _evaluar(
    almacen: Almacen, kpi: DefinicionKPI, ejecucion_id: str | None, ahora: datetime
) -> tuple[float | None, str]:
    disponibles: dict[str, Any] = {
        "ejec": ejecucion_id or "",
        "ahora": ahora.isoformat(sep=" ", timespec="seconds"),
        **kpi.parametros,
    }


    usados = set(_RE_PARAM.findall(kpi.sql))
    faltantes = usados - disponibles.keys()
    if faltantes:
        log.error("KPI '%s': parámetros sin declarar %s", kpi.id, sorted(faltantes))
        return None, "error"

    try:
        df = almacen.sql(kpi.sql, {k: v for k, v in disponibles.items() if k in usados})
    except Exception as exc:
        log.error("KPI '%s' falló: %s", kpi.id, exc)
        return None, "error"

    if df.empty or df.iloc[0, 0] is None or pd.isna(df.iloc[0, 0]):
        return None, "sin dato"
    return float(df.iloc[0, 0]), "ok"


def _movimiento(valor: float | None, anterior: float | None, direccion: str) -> str:
    if valor is None or anterior is None:
        return "sin base"
    delta = valor - anterior
    if abs(delta) < 1e-9 or (anterior and abs(delta / anterior) < 0.005):
        return "estable"
    mejora = delta > 0 if direccion == "mayor_mejor" else delta < 0
    return "mejora" if mejora else "empeora"


def calcular(
    almacen: Almacen,
    catalogo: list[DefinicionKPI],
    ejecucion_id: str | None = None,
    dias_tendencia: int = 28,
    ahora: datetime | None = None,
) -> pd.DataFrame:
    ahora = ahora or _ahora_utc()

    if ejecucion_id is None:
        ultima = almacen.ultima_ejecucion()
        if ultima is None:
            log.warning("El almacén no tiene corridas registradas")
            return pd.DataFrame()
        ejecucion_id = str(ultima["ejecucion_id"])

    referencia = almacen.ejecucion_de_referencia(ejecucion_id, dias_tendencia)

    filas = []
    for kpi in catalogo:
        valor, estado = _evaluar(almacen, kpi, ejecucion_id, ahora)
        anterior = _evaluar(almacen, kpi, referencia, ahora)[0] if referencia else None

        if valor is None or kpi.meta is None:
            cumple = None
        elif kpi.direccion == "mayor_mejor":
            cumple = valor >= kpi.meta
        else:
            cumple = valor <= kpi.meta

        filas.append(
            {
                "bloque": kpi.bloque,
                "kpi_id": kpi.id,
                "nombre": kpi.nombre,
                "valor": valor,
                "unidad": kpi.unidad,
                "meta": kpi.meta,
                "cumple": cumple,
                "valor_anterior": anterior,
                "delta": None if (valor is None or anterior is None) else valor - anterior,
                "movimiento": _movimiento(valor, anterior, kpi.direccion),
                "estado": estado,
                "dueno": kpi.dueno,
                "pregunta": kpi.pregunta,
            }
        )

    orden = {"alcance": 0, "captura": 1, "cumplimiento": 2, "operacion": 3, "valor": 4}
    tablero = pd.DataFrame(filas)
    tablero["_o"] = tablero["bloque"].map(orden).fillna(9)
    return tablero.sort_values(["_o", "kpi_id"]).drop(columns="_o").reset_index(drop=True)


def serie(almacen: Almacen, metrica: str, dias: int = 180) -> pd.DataFrame:
    return almacen.sql(
        """
        SELECT fecha, valor
        FROM v_metricas
        WHERE metrica = $metrica
          AND fecha > CAST($desde AS DATE)
        ORDER BY fecha
        """,
        {"metrica": metrica, "desde": (_ahora_utc().date() - pd.Timedelta(days=dias)).isoformat()},
    )


def marcador_fuentes(almacen: Almacen, ejecucion_id: str | None = None) -> pd.DataFrame:
    if ejecucion_id is None:
        ultima = almacen.ultima_ejecucion()
        if ultima is None:
            return pd.DataFrame()
        ejecucion_id = str(ultima["ejecucion_id"])

    return almacen.sql(
        """
        WITH actual AS (
            SELECT fuente, capturados, invalidos, contactables, captura_limpia, fecha
            FROM v_calidad_fuente WHERE ejecucion_id = $ejec
        ),
        base AS (
            SELECT c.fuente, avg(c.captura_limpia) AS limpia_previa
            FROM v_calidad_fuente c, actual a
            WHERE c.fuente = a.fuente
              AND c.fecha BETWEEN a.fecha - 56 AND a.fecha - 1
            GROUP BY 1
        )
        SELECT
            a.fuente,
            a.capturados,
            a.invalidos                                   AS perdida_absoluta,
            a.contactables,
            round(a.captura_limpia, 4)                    AS captura_limpia,
            round(b.limpia_previa, 4)                     AS captura_limpia_previa,
            round(a.captura_limpia - b.limpia_previa, 4)  AS variacion
        FROM actual a
        LEFT JOIN base b USING (fuente)
        ORDER BY a.invalidos DESC
        """,
        {"ejec": ejecucion_id},
    )
