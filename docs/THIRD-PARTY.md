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

## Bundled fonts

The web UI ships with Source Sans 3 (Adobe) and IBM Plex Mono (IBM), Latin subset, so no font is
loaded from the internet. Both are licensed under the SIL Open Font License 1.1. The license texts are in
`src/avidbackup/webui/static/fonts/` (`OFL-source-sans-3.txt`, `OFL-ibm-plex.txt`). The WOFF2 files
come from the Fontsource packages mirrored on jsDelivr.

