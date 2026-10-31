# Contributing

## Dev setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests
pytest
```

## Adding a database dumper

1. Implement `DbDumper` in `src/avidbackup/databases/yourdb.py`
2. Register in `databases/registry.py`
3. Add UI type option in `webui/templates/account_form.html`
4. Add tests under `tests/`
5. Document in `docs/CONFIGURATION.md` and README

Never commit secrets or client `data/` trees.
