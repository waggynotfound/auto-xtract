# Changelog

Versions follow [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.
The version lives in the `VERSION` file and is used by the build script, the
executable's file properties and the installer.

## 1.0.0 - 2026-11-23

First packaged release.

- Windows installer (`AutoXtract-1.0.0-Setup.exe`): installs to `Program Files`,
  Start Menu shortcut, optional desktop shortcut, Apps & features entry, clean
  uninstaller. Offers a per-user install (no admin) on the privileges page.
- Portable `AutoXtract.exe` built from source with PyInstaller, with product
  name, version and copyright in its file properties.
- Portable zip with the executable, README and changelog.
- `SHA256SUMS.txt` for both downloads.
- Release tooling: `build_release.ps1` and a GitHub Actions workflow that
  builds and attaches all three files to a tagged release.
- Installer points at the 7-Zip download page when 7-Zip is missing, since
  `.7z` and `.rar` need it.
