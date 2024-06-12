PHONY: run install sort


install:
	@poetry install --no-root --group dev

run: 
	@echo Visit http://127.0.0.1:8000/docs for the API Documentation
	@echo Visit http://127.0.0.1:8000/manifest for the Stremio Manifest
	@poetry run uvicorn comet.main:app --reload --reload-dir comet --reload-include "*.py" --reload-exclude "*.log"

sort:
	@poetry run isort comet
