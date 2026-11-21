## Install

**Windows:** download `AvidBackup-Setup-*.exe` below and run it. No Python needed.
The installer is not code-signed yet, so Windows SmartScreen may say "unrecognized app":
choose **More info, then Run anyway**. To verify the download, compare its SHA256 with the
`.sha256` file: `certutil -hashfile AvidBackup-Setup-*.exe SHA256` on Windows, or `sha256sum -c AvidBackup-Setup-*.exe.sha256` (Git Bash, Linux, macOS) from the download folder.

**Linux / macOS / any OS with Python 3.10+:**

```bash
pipx install git+https://github.com/nks16june/avidbackup.git
avidbackup app
```

Linux and macOS are experimental: they pass the automated tests but have no installer yet.
See the [README](https://github.com/nks16june/avidbackup#install) for Docker and from-source options.

Found a problem? [Open an issue](https://github.com/nks16june/avidbackup/issues).
Security issues: please report privately, see [SECURITY.md](https://github.com/nks16june/avidbackup/blob/main/SECURITY.md).

---

## What's changed
