from __future__ import annotations

import logging
import os
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from .modelo import TABLAS, VISTAS_DERIVADAS, Tabla, sql_vista_vacia

log = logging.getLogger("faro.almacen")


class Almacen:

    def __init__(self, raiz: str | Path):
        self.raiz = Path(raiz)
        self.raiz.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(database=":memory:")
        try:


            self._con.execute("SET enable_progress_bar = false")
        except duckdb.Error as exc:
            log.debug("No se pudo desactivar la barra de progreso: %s", exc)


        self._vistas_al_dia = False


    def ruta(self, tabla: str) -> Path:
        t = TABLAS[tabla]
        return self.raiz / tabla if t.particionada else self.raiz / f"{tabla}.parquet"

    def existe(self, tabla: str) -> bool:
        ruta = self.ruta(tabla)
        if TABLAS[tabla].particionada:
            return ruta.is_dir() and any(ruta.rglob("*.parquet"))
        return ruta.is_file()


    def anexar(self, tabla: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0

        self.invalidar_vistas()
        spec = TABLAS[tabla]
        nuevo = self._conformar(df, spec)

        if spec.particionada:
            return self._anexar_particionado(spec, nuevo)

        if self.existe(tabla):
            actual = pd.read_parquet(self.ruta(tabla))
            actual = self._descartar_llaves_repetidas(actual, nuevo, spec.llaves)
            combinado = pd.concat([actual, nuevo], ignore_index=True)
        else:
            combinado = nuevo

        self._escribir_atomico(combinado, self.ruta(tabla))
        log.info("%s: %d filas anexadas (total %d)", tabla, len(nuevo), len(combinado))
        return len(nuevo)

    def _anexar_particionado(self, spec: Tabla, nuevo: pd.DataFrame) -> int:
        escritas = 0
        for fecha, grupo in nuevo.groupby("fecha"):
            destino = self.ruta(spec.nombre) / f"fecha={fecha}"
            destino.mkdir(parents=True, exist_ok=True)
            archivo = destino / "parte.parquet"
            if archivo.exists():
                previo = pd.read_parquet(archivo)
                previo = self._descartar_llaves_repetidas(previo, grupo, spec.llaves)
                grupo = pd.concat([previo, grupo], ignore_index=True)
            self._escribir_atomico(grupo, archivo)
            escritas += len(grupo)
        return escritas

    @staticmethod
    def _descartar_llaves_repetidas(
        actual: pd.DataFrame, nuevo: pd.DataFrame, llaves: tuple[str, ...]
    ) -> pd.DataFrame:
        if actual.empty:
            return actual
        faltan = [k for k in llaves if k not in actual.columns]
        if faltan:
            return actual
        idx_nuevo = pd.MultiIndex.from_frame(nuevo[list(llaves)].astype(str))
        idx_actual = pd.MultiIndex.from_frame(actual[list(llaves)].astype(str))
        return actual[~idx_actual.isin(idx_nuevo)]

    @staticmethod
    def _conformar(df: pd.DataFrame, spec: Tabla) -> pd.DataFrame:
        faltantes = set(spec.llaves) - set(df.columns)
        if faltantes:
            raise ValueError(f"{spec.nombre}: faltan columnas llave {sorted(faltantes)}")
        salida = pd.DataFrame(index=df.index)
        for col in spec.columnas:
            salida[col] = df[col] if col in df.columns else pd.NA
        return salida.reset_index(drop=True)

    @staticmethod
    def _escribir_atomico(df: pd.DataFrame, destino: Path) -> None:
        destino.parent.mkdir(parents=True, exist_ok=True)
        tmp = destino.with_suffix(destino.suffix + ".tmp")
        df.to_parquet(tmp, index=False, compression="snappy")
        os.replace(tmp, destino)


    def invalidar_vistas(self) -> None:
        self._vistas_al_dia = False

    def _registrar_vistas(self) -> None:
        if self._vistas_al_dia:
            return
        for nombre, spec in TABLAS.items():
            if self.existe(nombre):
                patron = (
                    (self.ruta(nombre) / "**" / "*.parquet").as_posix()
                    if spec.particionada
                    else self.ruta(nombre).as_posix()
                )
                cuerpo = f"SELECT * FROM read_parquet('{patron}', union_by_name = true)"
            else:
                cuerpo = sql_vista_vacia(spec)
            self._con.execute(f"CREATE OR REPLACE VIEW v_{nombre} AS {cuerpo}")
        for nombre, cuerpo in VISTAS_DERIVADAS.items():
            self._con.execute(f"CREATE OR REPLACE VIEW v_{nombre} AS {cuerpo}")
        self._vistas_al_dia = True

    def sql(self, consulta: str, parametros: dict[str, Any] | None = None) -> pd.DataFrame:
        self._registrar_vistas()
        if parametros:
            return self._con.execute(consulta, parametros).df()
        return self._con.execute(consulta).df()

    def leer(self, tabla: str) -> pd.DataFrame:
        return self.sql(f"SELECT * FROM v_{tabla}")


    def ultima_ejecucion(self) -> dict[str, Any] | None:
        df = self.sql(
            "SELECT * FROM v_ejecuciones ORDER BY publicado_utc DESC NULLS LAST LIMIT 1"
        )
        return None if df.empty else df.iloc[0].to_dict()

    def ejecucion_de_referencia(self, ejecucion_id: str, dias_atras: int) -> str | None:
        df = self.sql(
            """
            WITH actual AS (SELECT fecha FROM v_ejecuciones WHERE ejecucion_id = $ejec)
            SELECT e.ejecucion_id
            FROM v_ejecuciones e, actual a
            WHERE e.fecha <= a.fecha - CAST($dias AS INTEGER)
            ORDER BY e.fecha DESC
            LIMIT 1
            """,
            {"ejec": ejecucion_id, "dias": dias_atras},
        )
        return None if df.empty else str(df.iloc[0]["ejecucion_id"])

    def purgar(self, hoy: date | None = None) -> dict[str, int]:
        hoy = hoy or date.today()
        self.invalidar_vistas()
        borrado: dict[str, int] = {}
        for nombre, spec in TABLAS.items():
            if spec.retencion_dias is None or not self.existe(nombre):
                continue
            limite = hoy - timedelta(days=spec.retencion_dias)
            if spec.particionada:
                n = 0
                for parte in sorted(self.ruta(nombre).glob("fecha=*")):
                    if pd.to_datetime(parte.name.split("=", 1)[1]).date() < limite:
                        shutil.rmtree(parte)
                        n += 1
                borrado[nombre] = n
            else:
                df = pd.read_parquet(self.ruta(nombre))
                col = "ocurrido_utc" if "ocurrido_utc" in df.columns else "fecha"
                antes = len(df)
                df = df[pd.to_datetime(df[col]).dt.date >= limite]
                self._escribir_atomico(df, self.ruta(nombre))
                borrado[nombre] = antes - len(df)
        return borrado

    def resumen(self) -> pd.DataFrame:
        filas = []
        for nombre in TABLAS:
            n = int(self.sql(f"SELECT count(*) AS n FROM v_{nombre}").iloc[0]["n"])
            filas.append({"tabla": nombre, "filas": n, "existe": self.existe(nombre)})
        return pd.DataFrame(filas)
