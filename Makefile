.PHONY: setup run tablero fuentes ordenes digest verificar limpiar

PY := python
CLAVE := $(if $(PHONE_HMAC_KEY),$(PHONE_HMAC_KEY),clave-local-de-desarrollo)

setup:
	$(PY) -m pip install -r requirements.txt
	$(PY) -m ipykernel install --user --name python3

run:
	@mkdir -p build
	PHONE_HMAC_KEY=$(CLAVE) papermill notebooks/pipeline_telefonos.ipynb build/ejecucion.ipynb \
		--cwd notebooks \
		-p PUBLICAR True \
		-p RUTA_GATES ../conf/quality_gates.ci.yml

tablero:
	PYTHONPATH=src $(PY) -m faro tablero

fuentes:
	PYTHONPATH=src $(PY) -m faro fuentes

ordenes:
	PYTHONPATH=src $(PY) -m faro ordenes ciclo

digest:
	PYTHONPATH=src $(PY) -m faro digest

verificar:
	@echo "== artefactos publicados =="
	@ls -1 data/gold/v=*/fecha_proceso=*/ 2>/dev/null || echo "FALTA data/gold"
	@echo "== almacen del observatorio =="
	@PYTHONPATH=src $(PY) -m faro resumen
	@echo "== indicadores =="
	@PYTHONPATH=src $(PY) -m faro tablero

limpiar:
	rm -rf data/gold data/observatorio build reports
