# Financial Student Lecture

A practical collection of finance-focused Jupyter notebooks for students who want hands-on practice with core quantitative finance topics.

## Project Description

This repository includes workshop-style notebooks covering foundational and applied topics in financial engineering and risk analysis, including:

- Introduction to financial engineering concepts
- Fixed income securities basics
- Multi-period binomial models
- Real-world option pricing with market data (`yfinance`)
- Credit risk workshop material
- Regression analysis in finance
- Value at Risk (VaR) modeling approaches

Notebooks are located in the `notebook/` directory.

## Environment Setup

This project uses **Python** and can be managed with either `uv` (recommended) or plain `venv + pip`.

### Option A: Setup with `uv` (recommended)

1. Install `uv` (if you do not have it):

   ```bash
   pip install uv
   ```

2. Create and sync the project environment:

   ```bash
   uv sync
   ```

3. Activate the virtual environment:

   ```bash
   source .venv/bin/activate
   ```

### Option B: Setup with `venv` + `pip`

1. Create a virtual environment:

   ```bash
   python -m venv .venv
   ```

2. Activate it:

   ```bash
   source .venv/bin/activate
   ```

3. Install dependencies from `pyproject.toml` (or your exported requirements if used):

   ```bash
   pip install -e .
   ```

## How to Run the Notebooks

After environment setup and activation:

1. Start Jupyter:

   ```bash
   jupyter notebook
   ```

   or

   ```bash
   jupyter lab
   ```

2. Open the `notebook/` folder in the Jupyter UI.
3. Select and run any notebook, for example:
   - `notebook/nb_intro_to_fin_eng_test.ipynb`
   - `notebook/nb_real_world_option_pricing_yfinance.ipynb`

## Quick Start

```bash
uv sync
source .venv/bin/activate
jupyter notebook
```

## Notes

- Some notebooks may fetch external market data (e.g., with `yfinance`) and require internet access.
- If a notebook kernel does not appear, ensure your virtual environment is active and Jupyter is installed in that environment.
