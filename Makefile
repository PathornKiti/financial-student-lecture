UV ?= uv
VENV ?= .venv
NOTEBOOK_DIR ?= notebook
JUPYTER_HOST ?= 127.0.0.1
JUPYTER_PORT ?= 8888
JUPYTER_PACKAGES := --with jupyterlab --with notebook --with ipykernel

.PHONY: help setup venv sync notebook lab clean

help: ## Show available Make targets.
	@awk 'BEGIN {FS = ":.*##"; printf "Available targets:\n"} /^[a-zA-Z_-]+:.*##/ {printf "  %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: venv sync ## Create the uv virtual environment and install project dependencies.
	@echo "Setup complete. Run 'make notebook' or 'make lab' to start Jupyter."

venv: ## Create a local virtual environment with uv.
	$(UV) venv $(VENV)

sync: ## Sync dependencies from pyproject.toml and uv.lock into the virtual environment.
	$(UV) sync

notebook: ## Start Jupyter Notebook in the notebook directory.
	$(UV) run $(JUPYTER_PACKAGES) jupyter notebook $(NOTEBOOK_DIR) --ip=$(JUPYTER_HOST) --port=$(JUPYTER_PORT)

lab: ## Start JupyterLab in the notebook directory.
	$(UV) run $(JUPYTER_PACKAGES) jupyter lab $(NOTEBOOK_DIR) --ip=$(JUPYTER_HOST) --port=$(JUPYTER_PORT)

clean: ## Remove local virtual environment and notebook cache files.
	rm -rf $(VENV) .ipynb_checkpoints $(NOTEBOOK_DIR)/.ipynb_checkpoints
	find $(NOTEBOOK_DIR) -type d -name .ipynb_checkpoints -prune -exec rm -rf {} +
