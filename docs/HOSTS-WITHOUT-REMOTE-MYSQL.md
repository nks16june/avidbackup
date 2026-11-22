# Backing up databases on hosts that block remote MySQL

Many shared hosts only let the website itself talk to its database. AvidBackup's normal way of
dumping a database (connect to the database host from your computer) then fails with
"Can't connect to MySQL server" or a timeout, while the **file** backup works fine.

AvidBackup has three ways to reach a database. Pick the first one that works for you.

| Method | Use it when | You need | Databases |
|---|---|---|---|
| **Direct** (default) | The host allows remote MySQL (often after you allow your IP in the hosting panel) | Database host, user, password | MySQL, MariaDB, PostgreSQL |
| **SSH tunnel** | Remote MySQL is blocked but you have SSH access (VPS, many cPanel hosts with "Shell access") | SSH login, plus the database login | MySQL, MariaDB, PostgreSQL |
| **PHP helper** | Neither remote MySQL nor SSH is available | The site's FTP/SFTP login, an HTTPS site, PHP with `mysqli` | MySQL, MariaDB |

Set it per database: open the site, then **Connection: direct · change** next to the database
(or go to `/accounts/<site>/db/<database>/connection`). **Test the connection** shows what happened.
From a terminal: `avidbackup db-test --account SITE`.

## SSH tunnel

AvidBackup opens an SSH connection (same host-key checking as SFTP: trust on first use, with a
strict option) and forwards a local port to the database as seen from the server. The dump then
runs through that port, so the database never has to be reachable from the internet.

1. Choose **SSH tunnel**.
2. Enter the SSH host, port, user and a password **or** a private key file. If the site's file
   access is already SFTP, tick **Use the site's SFTP login** instead.
3. Set **Database host, as seen from the SSH server**: usually `127.0.0.1` (or `localhost`), and the
   database port (3306 for MySQL, 5432 for PostgreSQL). Keep the database user, password and name
   as usual.
4. Save, then **Test the connection**.

In `accounts.json` (see [CONFIGURATION.md](CONFIGURATION.md)):

```json
{ "id": "main", "type": "mysql", "via": "ssh", "host": "127.0.0.1", "port": 3306,
  "user": "dbuser", "password_env": "SHOP_DB_PASSWORD", "database": "shop",
  "ssh_host": "ssh.example.com", "ssh_user": "me", "ssh_key_file": "C:\\Users\\me\\.ssh\\id_ed25519" }
```

Prefer `ssh_password_env` / `password_env` over literal passwords, and a key file over a password.

## PHP helper

AvidBackup generates **one small PHP file** for **one database**, uploads it to your site over the
site's FTP/SFTP login, and then downloads a gzip-compressed SQL dump from it over HTTPS.

```
avidbackup helper install --account SITE --db main
avidbackup helper test    --account SITE --db main
avidbackup run --account SITE --db-only
avidbackup helper remove  --account SITE --db main     # when you no longer need it
```

(Or use the buttons on the connection page.) `install` uploads the file, then proves it answers over
HTTPS. If it cannot be reached it is deleted again and you get an explanation. Options:
`--url https://example.com/blog/` if the upload folder is served at a different address than the
site URL, and `--allowed-ip 203.0.113.7` to accept requests only from your own address.

### What the helper does, and does not do

- It sends a **read-only** dump of the one configured database (tables, data, views, triggers,
  stored routines) from a single consistent snapshot. It never writes to the database.
- It **runs no shell commands** and never lists or reads files.
- It only answers **POST** requests that carry the secret token **in the request body** (not the
  URL, so it does not appear in access logs). Many shared hosts strip the `Authorization` header,
  which is why a header is not used.
- The token is random (256 bits). Only its **SHA-256 hash** is stored in the PHP file; the token
  itself lives in your `accounts.json`. Comparison is constant-time.
- After 5 wrong tokens in 10 minutes the helper locks itself for 10 minutes, even for the right token.
- The file name is random (`avidbackup-<16 hex>.php`), replies are `no-store`, `noindex`.
- AvidBackup **refuses plain `http://`** helper URLs (except on this computer) so the token cannot
  cross the internet unencrypted, checks the TLS certificate, and refuses redirects (a redirect would
  drop the POST body).
- A dump that is cut off or has no completion marker is **discarded**, never saved as a good backup.

### Things to know

- **The file contains your database login**, just like `wp-config.php`. PHP executes it rather than
  showing it, but treat it as sensitive and **remove the helper when you stop using it**
  (`avidbackup helper remove`). If you suspect the token leaked, click **Reinstall** (new token, new
  file name; the old file is deleted).
- Needs PHP with the `mysqli` extension (almost every host has it). `zlib` makes the download
  smaller; without it the dump is sent uncompressed.
- **Very large databases** can hit host limits (PHP run time, web-server or proxy timeouts). The
  helper streams continuously and asks PHP for no time limit, but a host can still enforce one.
  If a big dump fails, use SSH or direct access instead.
- Behind **Cloudflare**, use the proxied `https://` address. "Flexible" SSL is fine because the
  helper does not need HTTPS on the origin.
- A web application firewall may block the POST. If `helper install` says the helper "did not
  answer", check the host's firewall log, or allow the file name.
- The helper backs up **MySQL / MariaDB only**. PostgreSQL needs direct access or an SSH tunnel.

## Which one should I choose?

1. Try **Direct** first: allow your IP in the hosting panel (look for "Remote MySQL").
2. If the host offers SSH, use the **SSH tunnel**. It needs nothing installed on the server and
   works for PostgreSQL too.
3. Otherwise use the **PHP helper**.

## Troubleshooting

| Message | Meaning |
|---|---|
| `Could not open the SSH connection ... HOST KEY MISMATCH` | The server's key changed. If it was legitimately reinstalled, remove its line from `config/known_hosts`. |
| `SSH server refused the connection to 127.0.0.1:3306` | Wrong database host/port as seen from the SSH server, or the host forbids port forwarding. |
| `Helper replied HTTP 403: the secret token was rejected` | Wrong token, or your address is not in the allow-list. Reinstall the helper. |
| `Helper replied HTTP 429` | Too many wrong tokens; wait 10 minutes. |
| `no helper was found at that address` | Wrong URL, or the file was removed. Reinstall. |
| `The helper URL must use https://` | Enable HTTPS on the site first; the token must not travel over plain http. |
| `The dump is incomplete ... discarded` | The connection dropped or the host stopped the script (time limit). Try SSH. |
