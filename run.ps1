param([string]$Accion = "run")

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$env:PYTHONPATH = "src"
if (-not $env:PHONE_HMAC_KEY) { $env:PHONE_HMAC_KEY = "clave-local-de-desarrollo" }

switch ($Accion) {
    "setup" {
        python -m pip install --upgrade pip
        python -m pip install -r requirements.txt
        python -m ipykernel install --user --name python3
    }
    "run" {
        New-Item -ItemType Directory -Force -Path build | Out-Null
        papermill notebooks/pipeline_telefonos.ipynb build/ejecucion.ipynb --cwd notebooks -p PUBLICAR True -p RUTA_GATES ../conf/quality_gates.ci.yml
    }
    "verificar" {
        Write-Host "== artefactos publicados ==" -ForegroundColor Cyan
        Get-ChildItem -Recurse -File data/gold | Select-Object FullName, Length
        Write-Host "== almacen del observatorio ==" -ForegroundColor Cyan
        python -m faro resumen
        Write-Host "== indicadores ==" -ForegroundColor Cyan
        python -m faro tablero
    }
    "tablero" { python -m faro tablero }
    "fuentes" { python -m faro fuentes }
    "ordenes" { python -m faro ordenes ciclo }
    "digest"  { python -m faro digest }
    "datos" {
        python -c "import pandas as pd, glob; pd.set_option('display.width', 200); print(pd.read_parquet(glob.glob('data/gold/**/telefonos.parquet', recursive=True)[0])[['cliente_id','telefono_enmascarado','tipo_linea','es_principal','apto_contacto','motivo_no_contacto']].to_string(index=False))"
    }
    "limpiar" {
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue data/gold, data/observatorio, build, reports
        Write-Host "Limpio."
    }
    default { Write-Host "Acciones: setup run verificar tablero fuentes ordenes digest datos limpiar" }
}
