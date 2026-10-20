dist/:
	mkdir -p $@

build: dist/
	rm -rf src/otelite/__pycache__
	python3 build_zipapp.py

lint:
	ruff check --select I --fix src/otelite/*.py tests/*.py
	ruff format src/otelite/*.py tests/*.py
	ruff check src/otelite tests
	-mypy src/otelite
	wc -l src/otelite/*.py | sort -n

test:
	PYTHONPATH=src python3 -m unittest discover -v -s tests -p "test_*.py"
