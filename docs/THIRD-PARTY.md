# Third-party components

## PostgreSQL client tools (optional, downloaded on demand)

AvidBackup does not ship PostgreSQL. If you choose to install the PostgreSQL
tools (installer checkbox, Settings page, or `avidbackup tools install-postgres`),
it downloads `pg_dump`, `psql`, `pg_restore` and their DLLs from the official
PostgreSQL Windows binaries archive published at
https://www.enterprisedb.com/download-postgresql-binaries and verifies each file
against a SHA256 pin in `src/avidbackup/data/pgtools_manifest.json`.

The PostgreSQL license and the third-party licenses for the command-line tools
are saved next to the tools (`server_license.txt`,
`commandlinetools_3rd_party_licenses.txt`).

To update the pinned version, replace the URL, sizes and hashes in the manifest
and run the tests.
