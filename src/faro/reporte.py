from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from .almacen import Almacen
from . import ordenes as mod_ordenes

_UNIDADES = {
    "porcentaje": lambda v: f"{v:.1%}",
    "clientes": lambda v: f"{v:,.0f}".replace(",", "."),
    "telefonos": lambda v: f"{v:,.0f}".replace(",", "."),
    "horas": lambda v: f"{v:.0f} h",
    "moneda": lambda v: "$" + f"{v:,.0f}".replace(",", "."),
}

_SEMAFORO = {True: "cumple", False: "INCUMPLE", None: "—"}
_FLECHA = {"mejora": "▲ mejora", "empeora": "▼ empeora", "estable": "= estable", "sin base": "—"}


def formatear(valor: float | None, unidad: str) -> str:
    if valor is None or pd.isna(valor):
        return "sin dato"
    return _UNIDADES.get(unidad, lambda v: f"{v:,.4g}")(valor)


def tablero_texto(kpis: pd.DataFrame) -> str:
    if kpis.empty:
        return "El observatorio todavía no tiene corridas registradas."

    lineas: list[str] = []
    for bloque, grupo in kpis.groupby("bloque", sort=False):
        lineas.append(f"\n{bloque.upper()}")
        lineas.append("─" * 78)
        for _, k in grupo.iterrows():
            estado = _SEMAFORO[k["cumple"]] if k["cumple"] is not None else "—"
            meta = f"meta {formatear(k['meta'], k['unidad'])}" if pd.notna(k["meta"]) else ""
            lineas.append(
                f"  {k['nombre'][:38]:<38} {formatear(k['valor'], k['unidad']):>13}  "
                f"{estado:<9} {_FLECHA.get(k['movimiento'], ''):<11} {meta}"
            )
    return "\n".join(lineas)


def digest_fuente(
    almacen: Almacen,
    fuente: str,
    config: dict[str, Any],
    hoy: date | None = None,
) -> str:
    hoy = hoy or date.today()
    dueno = mod_ordenes._dueno_de(config, fuente)

    historia = almacen.sql(
        """
        SELECT fecha, capturados, invalidos, round(captura_limpia, 4) AS captura_limpia
        FROM v_calidad_fuente WHERE fuente = $f ORDER BY fecha DESC LIMIT 8
        """,
        {"f": fuente},
    )
    if historia.empty:
        return f"# {fuente}\n\nSin datos registrados para esta fuente."

    actual = historia.iloc[0]
    previa = historia["captura_limpia"].iloc[1:].mean() if len(historia) > 1 else None

    partes = [
        f"# Calidad de captura — {fuente}",
        f"\n**Para:** {dueno}  ",
        f"**Corte:** {actual['fecha']}",
        "\n## Cómo va",
        f"\n- Captura limpia: **{actual['captura_limpia']:.1%}**"
        + (f" (promedio de las corridas anteriores: {previa:.1%})" if previa is not None else ""),
        f"- Teléfonos capturados en la última corrida: {int(actual['capturados']):,}".replace(",", "."),
        f"- **Teléfonos perdidos por captura inválida: {int(actual['invalidos']):,}**".replace(",", "."),
    ]

    motivos = almacen.sql(
        """
        SELECT motivo, sum(registros) AS registros
        FROM v_rechazos_clasificado
        WHERE fuente = $f AND categoria = 'invalido'
          AND ejecucion_id = (SELECT ejecucion_id FROM v_ejecuciones
                              ORDER BY publicado_utc DESC LIMIT 1)
        GROUP BY 1 ORDER BY 2 DESC
        """,
        {"f": fuente},
    )
    if not motivos.empty:
        partes.append("\n## Por qué se pierden\n")
        partes.append("| Motivo | Teléfonos |")
        partes.append("|---|---:|")
        for _, m in motivos.iterrows():
            partes.append(f"| {m['motivo']} | {int(m['registros']):,}".replace(",", ".") + " |")

    abiertas = mod_ordenes.listar(almacen, hoy=hoy)
    mias = abiertas[abiertas["fuente"] == fuente] if not abiertas.empty else pd.DataFrame()
    partes.append("\n## Qué hay que corregir\n")
    if mias.empty:
        partes.append("Sin órdenes abiertas. Nada pendiente de su parte.")
    else:
        partes.append("| Orden | Severidad | Hallazgo | Vence |")
        partes.append("|---|---|---|---|")
        for _, o in mias.iterrows():
            plazo = "**VENCIDA**" if o["vencida"] else f"en {int(o['dias_para_vencer'])} días"
            partes.append(f"| {o['orden_id']} | {o['severidad']} | {o['hallazgo']} | {plazo} |")
        partes.append(
            "\n> La corrección se hace **en el sistema origen**, no en el dataset publicado: "
            "la siguiente corrida sobrescribiría cualquier parche. La orden se cierra sola "
            "cuando el pipeline verifique que el problema desapareció."
        )

    return "\n".join(partes)


def digest_general(kpis: pd.DataFrame, ordenes: pd.DataFrame, hoy: date | None = None) -> str:
    hoy = hoy or date.today()
    partes = [f"# Estado del dataset de teléfonos — {hoy.isoformat()}", ""]

    if not kpis.empty:
        titular = kpis[kpis["kpi_id"] == "alcance_contactable"]
        if not titular.empty:
            t = titular.iloc[0]
            partes.append(
                f"**Alcance contactable: {formatear(t['valor'], t['unidad'])}** "
                f"({_FLECHA.get(t['movimiento'], '')} frente a hace 28 días, "
                f"meta {formatear(t['meta'], t['unidad'])})\n"
            )
        incumplen = kpis[kpis["cumple"] == False]
        if incumplen.empty:
            partes.append("Todos los indicadores con meta se están cumpliendo.\n")
        else:
            partes.append(f"## Indicadores por debajo de la meta ({len(incumplen)})\n")
            partes.append("| Indicador | Valor | Meta | Responsable |")
            partes.append("|---|---:|---:|---|")
            for _, k in incumplen.iterrows():
                partes.append(
                    f"| {k['nombre']} | {formatear(k['valor'], k['unidad'])} "
                    f"| {formatear(k['meta'], k['unidad'])} | {k['dueno']} |"
                )
            partes.append("")

    partes.append(f"## Órdenes abiertas ({len(ordenes)})\n")
    if ordenes.empty:
        partes.append("Ninguna.")
    else:
        partes.append("| Orden | Sev. | Fuente | Responsable | Vence |")
        partes.append("|---|---|---|---|---|")
        for _, o in ordenes.iterrows():
            plazo = "**VENCIDA**" if o["vencida"] else f"{int(o['dias_para_vencer'])} d"
            partes.append(
                f"| {o['orden_id']} | {o['severidad']} | {o['fuente']} | {o['dueno']} | {plazo} |"
            )

    return "\n".join(partes)
