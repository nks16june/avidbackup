# Contributing

## Dev setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests
pytest
```

### Integration tests (optional)

Most tests need nothing special. The PHP-helper and SSH-tunnel end-to-end tests also use a real PHP and
a throwaway MySQL server, and skip themselves when those are missing. To run them, point the tests at
your installs (the MySQL server is a temporary instance in a temp folder on a random port; it never
touches an installed MySQL):

```bash
export AVIDBACKUP_TEST_PHP=/path/to/php            # needs the mysqli extension
export AVIDBACKUP_TEST_MYSQLD=/path/to/mysqld
pytest tests/test_php_helper_integration.py tests/test_php_helper_security.py
```

## Adding a database dumper

1. Implement `DbDumper` in `src/avidbackup/databases/yourdb.py`
2. Register in `databases/registry.py`
3. Add UI type option in `webui/templates/account_form.html`
4. Add tests under `tests/`
5. Document in `docs/CONFIGURATION.md` and README

Never commit secrets or client `data/` trees.
