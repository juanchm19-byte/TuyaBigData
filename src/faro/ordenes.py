from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .almacen import Almacen
from .kpi import DefinicionKPI

log = logging.getLogger("faro.ordenes")

ABIERTA = "abierta"
CERRADA = "cerrada"


@dataclass(frozen=True)
class Hallazgo:

    regla: str
    severidad: str
    fuente: str
    hallazgo: str
    valor: float
    umbral: float
    registros: int
    dispara: bool


def cargar_config(ruta: str | Path) -> dict[str, Any]:
    return yaml.safe_load(Path(ruta).read_text(encoding="utf-8")) or {}


def _dueno_de(config: dict[str, Any], fuente: str) -> str:
    dueños = config.get("duenos_por_fuente", {}) or {}
    return dueños.get(fuente, config.get("dueno_por_defecto", "Gobierno de Datos"))


def identificar_orden(regla: str, fuente: str, creada: date) -> str:
    semilla = f"{regla}|{fuente}|{creada.isoformat()}"
    return "OC-" + hashlib.sha256(semilla.encode("utf-8")).hexdigest()[:8].upper()


def evaluar_reglas(
    almacen: Almacen,
    config: dict[str, Any],
    ejecucion_id: str,
    catalogo: list[DefinicionKPI] | None = None,
) -> list[Hallazgo]:
    hallazgos: list[Hallazgo] = []
    fuentes = almacen.sql(
        "SELECT * FROM v_calidad_fuente WHERE ejecucion_id = $ejec", {"ejec": ejecucion_id}
    )

    for regla in config.get("reglas", []) or []:
        tipo = regla["tipo"]

        if tipo == "caida_fuente":
            hallazgos += _regla_caida_fuente(almacen, regla, ejecucion_id, fuentes)
        elif tipo == "piso_fuente":
            hallazgos += _regla_piso_fuente(regla, fuentes)
        elif tipo == "volumen_motivo":
            hallazgos += _regla_volumen_motivo(almacen, regla, ejecucion_id)
        elif tipo == "metrica":
            hallazgos += _regla_metrica(almacen, regla, ejecucion_id)
        elif tipo == "kpi":
            hallazgos += _regla_kpi(almacen, regla, ejecucion_id, catalogo or [])
        else:
            raise ValueError(f"Tipo de regla desconocido: '{tipo}'")

    return hallazgos


def _regla_caida_fuente(
    almacen: Almacen, regla: dict[str, Any], ejecucion_id: str, fuentes: pd.DataFrame
) -> list[Hallazgo]:
    base = almacen.sql(
        """
        WITH actual AS (SELECT fecha FROM v_ejecuciones WHERE ejecucion_id = $ejec)
        SELECT c.fuente, avg(c.captura_limpia) AS previa, count(*) AS corridas
        FROM v_calidad_fuente c, actual a
        WHERE c.fecha BETWEEN a.fecha - CAST($dias AS INTEGER) AND a.fecha - 1
        GROUP BY 1
        """,
        {"ejec": ejecucion_id, "dias": int(regla.get("dias_base", 56))},
    )
    minimo = int(regla.get("min_corridas_base", 3))
    salida = []
    for _, f in fuentes.iterrows():
        ref = base[base["fuente"] == f["fuente"]]
        if ref.empty or int(ref.iloc[0]["corridas"]) < minimo:
            continue
        previa = float(ref.iloc[0]["previa"])
        caida = previa - float(f["captura_limpia"])
        salida.append(
            Hallazgo(
                regla=regla["id"],
                severidad=regla["severidad"],
                fuente=str(f["fuente"]),
                hallazgo=(
                    f"La captura limpia de «{f['fuente']}» cayó {caida * 100:.1f} puntos "
                    f"({previa:.1%} → {f['captura_limpia']:.1%}). "
                    f"{int(f['invalidos'])} teléfonos perdidos en esta corrida."
                ),
                valor=caida,
                umbral=float(regla["umbral"]),
                registros=int(f["invalidos"]),
                dispara=caida > float(regla["umbral"]),
            )
        )
    return salida


def _regla_piso_fuente(regla: dict[str, Any], fuentes: pd.DataFrame) -> list[Hallazgo]:
    umbral = float(regla["umbral"])
    return [
        Hallazgo(
            regla=regla["id"],
            severidad=regla["severidad"],
            fuente=str(f["fuente"]),
            hallazgo=(
                f"«{f['fuente']}» captura limpia {f['captura_limpia']:.1%}, por debajo del "
                f"mínimo aceptable de {umbral:.0%}. Pérdida absoluta: "
                f"{int(f['invalidos'])} teléfonos."
            ),
            valor=float(f["captura_limpia"]),
            umbral=umbral,
            registros=int(f["invalidos"]),
            dispara=float(f["captura_limpia"]) < umbral,
        )
        for _, f in fuentes.iterrows()
    ]


def _regla_volumen_motivo(
    almacen: Almacen, regla: dict[str, Any], ejecucion_id: str
) -> list[Hallazgo]:
    motivo = regla["motivo"]
    df = almacen.sql(
        """
        SELECT coalesce(sum(registros), 0) AS registros
        FROM v_rechazos WHERE ejecucion_id = $ejec AND motivo = $motivo
        """,
        {"ejec": ejecucion_id, "motivo": motivo},
    )
    registros = int(df.iloc[0]["registros"]) if not df.empty else 0
    umbral = float(regla["umbral"])
    return [
        Hallazgo(
            regla=regla["id"],
            severidad=regla["severidad"],
            fuente=regla.get("fuente", "transversal"),
            hallazgo=(
                f"{registros} registros con motivo «{motivo}» superan el umbral de "
                f"{int(umbral)}. {regla.get('accion', '')}".strip()
            ),
            valor=float(registros),
            umbral=umbral,
            registros=registros,
            dispara=registros > umbral,
        )
    ]


def _regla_metrica(
    almacen: Almacen, regla: dict[str, Any], ejecucion_id: str
) -> list[Hallazgo]:
    df = almacen.sql(
        "SELECT valor FROM v_metricas WHERE ejecucion_id = $ejec AND metrica = $metrica",
        {"ejec": ejecucion_id, "metrica": regla["metrica"]},
    )
    if df.empty:
        return []
    valor = float(df.iloc[0]["valor"])
    umbral = float(regla["umbral"])
    comparador = regla.get("comparador", "mayor")
    dispara = valor > umbral if comparador == "mayor" else valor < umbral
    return [
        Hallazgo(
            regla=regla["id"],
            severidad=regla["severidad"],
            fuente=regla.get("fuente", "transversal"),
            hallazgo=(
                f"{regla.get('descripcion', regla['metrica'])}: {valor:,.0f} "
                f"({'supera' if comparador == 'mayor' else 'está por debajo de'} "
                f"el umbral de {umbral:,.0f}). {regla.get('accion', '')}".strip()
            ),
            valor=valor,
            umbral=umbral,
            registros=int(valor),
            dispara=dispara,
        )
    ]


def _regla_kpi(
    almacen: Almacen, regla: dict[str, Any], ejecucion_id: str, catalogo: list[DefinicionKPI]
) -> list[Hallazgo]:
    from .kpi import _evaluar

    definicion = next((k for k in catalogo if k.id == regla["kpi"]), None)
    if definicion is None:
        raise ValueError(f"La regla '{regla['id']}' referencia un KPI inexistente: {regla['kpi']}")
    if definicion.meta is None:
        raise ValueError(f"El KPI '{definicion.id}' no tiene meta; no puede sostener una regla")

    valor, estado = _evaluar(almacen, definicion, ejecucion_id, datetime.now(timezone.utc).replace(tzinfo=None))
    if estado != "ok" or valor is None:
        return []

    incumple = (
        valor < definicion.meta if definicion.direccion == "mayor_mejor" else valor > definicion.meta
    )
    return [
        Hallazgo(
            regla=regla["id"],
            severidad=regla["severidad"],
            fuente=regla.get("fuente", "transversal"),
            hallazgo=(
                f"«{definicion.nombre}» en {valor:,.4g} frente a la meta de "
                f"{definicion.meta:,.4g}. {definicion.pregunta}"
            ),
            valor=float(valor),
            umbral=float(definicion.meta),
            registros=0,
            dispara=bool(incumple),
        )
    ]


def _abiertas(almacen: Almacen) -> pd.DataFrame:
    return almacen.sql(f"SELECT * FROM v_ordenes WHERE estado = '{ABIERTA}'")


def detectar(
    almacen: Almacen,
    config: dict[str, Any],
    catalogo: list[DefinicionKPI] | None = None,
    ejecucion_id: str | None = None,
    hoy: date | None = None,
) -> pd.DataFrame:
    hoy = hoy or date.today()
    if ejecucion_id is None:
        ultima = almacen.ultima_ejecucion()
        if ultima is None:
            return pd.DataFrame()
        ejecucion_id = str(ultima["ejecucion_id"])

    sla = config.get("sla_dias", {}) or {}
    abiertas = _abiertas(almacen)
    ya_abiertas = set(zip(abiertas.get("regla", []), abiertas.get("fuente", [])))

    nuevas = []
    for h in evaluar_reglas(almacen, config, ejecucion_id, catalogo):
        if not h.dispara or (h.regla, h.fuente) in ya_abiertas:
            continue
        nuevas.append(
            {
                "orden_id": identificar_orden(h.regla, h.fuente, hoy),
                "regla": h.regla,
                "severidad": h.severidad,
                "fuente": h.fuente,
                "hallazgo": h.hallazgo,
                "dueno": _dueno_de(config, h.fuente),
                "registros": h.registros,
                "valor_detectado": h.valor,
                "umbral": h.umbral,
                "creada": hoy,
                "vence": hoy + timedelta(days=int(sla.get(h.severidad, 15))),
                "estado": ABIERTA,
                "cerrada": None,
                "evidencia_cierre": None,
                "ejecucion_deteccion": ejecucion_id,
            }
        )

    df = pd.DataFrame(nuevas)
    if not df.empty:
        almacen.anexar("ordenes", df)
        log.info("Órdenes creadas: %d", len(df))
    return df


def verificar_cierre(
    almacen: Almacen,
    config: dict[str, Any],
    catalogo: list[DefinicionKPI] | None = None,
    ejecucion_id: str | None = None,
    hoy: date | None = None,
) -> pd.DataFrame:
    hoy = hoy or date.today()
    if ejecucion_id is None:
        ultima = almacen.ultima_ejecucion()
        if ultima is None:
            return pd.DataFrame()
        ejecucion_id = str(ultima["ejecucion_id"])

    abiertas = _abiertas(almacen)
    if abiertas.empty:
        return pd.DataFrame()

    vigentes = {
        (h.regla, h.fuente): h
        for h in evaluar_reglas(almacen, config, ejecucion_id, catalogo)
        if h.dispara
    }

    cerradas = []
    for _, orden in abiertas.iterrows():
        clave = (orden["regla"], orden["fuente"])
        if clave in vigentes:
            continue
        fila = orden.to_dict()
        fila.update(
            {
                "estado": CERRADA,
                "cerrada": hoy,
                "evidencia_cierre": (
                    f"La regla «{orden['regla']}» dejó de dispararse en la corrida "
                    f"{ejecucion_id}. Verificado automáticamente por el pipeline."
                ),
            }
        )
        cerradas.append(fila)

    df = pd.DataFrame(cerradas)
    if not df.empty:
        almacen.anexar("ordenes", df)
        log.info("Órdenes cerradas automáticamente: %d", len(df))
    return df


def listar(almacen: Almacen, estado: str | None = ABIERTA, hoy: date | None = None) -> pd.DataFrame:
    hoy = hoy or date.today()
    df = almacen.leer("ordenes")
    if df.empty:
        return df
    if estado:
        df = df[df["estado"] == estado]
    if df.empty:
        return df

    prioridad = {"critica": 0, "alta": 1, "media": 2, "baja": 3}
    df = df.copy()
    df["_p"] = df["severidad"].map(prioridad).fillna(9)
    df["dias_para_vencer"] = (pd.to_datetime(df["vence"]).dt.date - hoy).map(lambda d: d.days)
    df["vencida"] = df["dias_para_vencer"] < 0
    return df.sort_values(["_p", "dias_para_vencer"]).drop(columns="_p").reset_index(drop=True)
