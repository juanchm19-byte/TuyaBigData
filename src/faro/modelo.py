from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tabla:
    nombre: str
    grano: str
    llaves: tuple[str, ...]
    columnas: dict[str, str]
    particionada: bool = False
    retencion_dias: int | None = None


EJECUCIONES = Tabla(
    nombre="ejecuciones",
    grano="una fila por corrida del pipeline",
    llaves=("ejecucion_id",),
    columnas={
        "ejecucion_id": "VARCHAR",
        "fecha": "DATE",
        "publicado_utc": "TIMESTAMP",
        "version_dataset": "VARCHAR",
        "contrato": "VARCHAR",
        "entorno": "VARCHAR",
        "git_sha": "VARCHAR",
        "sha256": "VARCHAR",
        "filas": "BIGINT",
    },
)

METRICAS = Tabla(
    nombre="metricas",
    grano="una fila por métrica y corrida (formato largo)",
    llaves=("ejecucion_id", "metrica"),
    columnas={
        "ejecucion_id": "VARCHAR",
        "fecha": "DATE",
        "metrica": "VARCHAR",
        "valor": "DOUBLE",
    },
)


RECHAZOS = Tabla(
    nombre="rechazos",
    grano="fuente x motivo x corrida (agregado sobre CANDIDATOS, antes de deduplicar)",
    llaves=("ejecucion_id", "fuente", "motivo"),
    columnas={
        "ejecucion_id": "VARCHAR",
        "fecha": "DATE",
        "fuente": "VARCHAR",
        "motivo": "VARCHAR",
        "registros": "BIGINT",
        "clientes": "BIGINT",
    },
)

RECHAZOS_DETALLE = Tabla(
    nombre="rechazos_detalle",
    grano="un registro rechazado por corrida",
    llaves=("ejecucion_id", "telefono_hash", "motivo"),
    columnas={
        "ejecucion_id": "VARCHAR",
        "fecha": "DATE",
        "cliente_id": "VARCHAR",
        "telefono_hash": "VARCHAR",
        "telefono_enmascarado": "VARCHAR",
        "fuente": "VARCHAR",
        "motivo": "VARCHAR",
    },
    particionada=True,
    retencion_dias=90,
)

ENTREGABILIDAD = Tabla(
    nombre="entregabilidad",
    grano="un evento de contacto real",
    llaves=("evento_id",),
    columnas={
        "evento_id": "VARCHAR",
        "telefono_hash": "VARCHAR",
        "evento": "VARCHAR",
        "canal": "VARCHAR",
        "ocurrido_utc": "TIMESTAMP",
    },
    retencion_dias=365,
)

ORDENES = Tabla(
    nombre="ordenes",
    grano="un hallazgo asignado a un responsable",
    llaves=("orden_id",),
    columnas={
        "orden_id": "VARCHAR",
        "regla": "VARCHAR",
        "severidad": "VARCHAR",
        "fuente": "VARCHAR",
        "hallazgo": "VARCHAR",
        "dueno": "VARCHAR",
        "registros": "BIGINT",
        "valor_detectado": "DOUBLE",
        "umbral": "DOUBLE",
        "creada": "DATE",
        "vence": "DATE",
        "estado": "VARCHAR",
        "cerrada": "DATE",
        "evidencia_cierre": "VARCHAR",
        "ejecucion_deteccion": "VARCHAR",
    },
)

TABLAS: dict[str, Tabla] = {
    t.nombre: t
    for t in (EJECUCIONES, METRICAS, RECHAZOS, RECHAZOS_DETALLE, ENTREGABILIDAD, ORDENES)
}


def sql_vista_vacia(tabla: Tabla) -> str:
    columnas = ", ".join(f"CAST(NULL AS {tipo}) AS {col}" for col, tipo in tabla.columnas.items())
    return f"SELECT {columnas} WHERE 1 = 0"


MOTIVOS_INVALIDOS = (
    "VACIO",
    "NO_PARSEABLE",
    "IMPOSIBLE",
    "INVALIDO",
    "REGION_NO_PERMITIDA",
    "SOSPECHOSO_PRUEBA",
)

_lista_invalidos = ", ".join(f"'{m}'" for m in MOTIVOS_INVALIDOS)

VISTAS_DERIVADAS: dict[str, str] = {
    "rechazos_clasificado": f"""
        SELECT *,
               CASE
                 WHEN motivo IN ({_lista_invalidos}) THEN 'invalido'
                 WHEN motivo = 'TIPO_NO_CONTACTABLE'  THEN 'no_apto'
                 WHEN motivo = 'SIN_CONSENTIMIENTO'   THEN 'sin_consentimiento'
                 WHEN motivo = 'EN_DNC'               THEN 'excluido'
                 WHEN motivo = 'OK'                   THEN 'contactable'
                 ELSE 'otro'
               END AS categoria
        FROM v_rechazos
    """,
    "calidad_fuente": """
        SELECT
            ejecucion_id,
            fecha,
            fuente,
            sum(registros)                                                        AS capturados,
            sum(CASE WHEN categoria = 'invalido'    THEN registros ELSE 0 END)    AS invalidos,
            sum(CASE WHEN categoria = 'contactable' THEN registros ELSE 0 END)    AS contactables,
            1.0 - sum(CASE WHEN categoria = 'invalido' THEN registros ELSE 0 END)
                  / NULLIF(sum(registros), 0)                                     AS captura_limpia
        FROM v_rechazos_clasificado
        GROUP BY 1, 2, 3
    """,
}
