# Packaging & releases

How the two editions ship. The **Android** edition is built and published by CI from a
`v*` tag; the **short-lab desktop** edition is packaged by CI from a `short-lab-v*` tag
(and on demand via manual dispatch). All of this is automated in
[`.github/workflows/release.yml`](../.github/workflows/release.yml); the per-push gates
live in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

Tag contract: `v*` is Android-only, `short-lab-v*` is Desktop-only. The Android version
does not move with Desktop tags.

## Android releases (tag → signed APK)

1. Get a `v*` tag onto GitHub (see [the push caveat](#this-machine-git-push-over-1-mb-is-broken)
   below for how that works on the owner's machine).
2. The `release` job in `release.yml` then:
   - decodes the `KEYSTORE_BASE64` secret to `android/app/release.keystore.jks`,
   - exports the signing env the Gradle scaffold already reads — `STORE_FILE`,
     `STORE_PASSWORD`, `KEY_ALIAS`, `KEY_PASSWORD` (see the signing block in
     [`android/app/build.gradle.kts`](../android/app/build.gradle.kts)),
   - runs `./gradlew :app:assembleRelease :app:bundleRelease` (R8-minified),
   - creates the GitHub Release for the tag and uploads the `.apk` and `.aab` from
     `android/app/build/outputs/{apk,bundle}/release/`.

### Signing secrets

| Secret | Meaning | Notes |
| --- | --- | --- |
| `KEYSTORE_BASE64` | base64 of `release.keystore.jks` | decoded by CI to `android/app/release.keystore.jks` |
| `KEYSTORE_PASSWORD` | keystore store password | mapped to Gradle's `STORE_PASSWORD` |
| `KEY_ALIAS` | signing key alias | mapped to Gradle's `KEY_ALIAS` |
| `KEY_PASSWORD` | signing key password | mapped to Gradle's `KEY_PASSWORD` |

Defaults & graceful degradation: `STORE_FILE` always points at `app/release.keystore.jks`
— the path CI decodes to, relative to `android/`. If any of the four values is blank
(e.g. the secrets are simply not configured), the Gradle scaffold skips the signing
config and CI **still publishes the release with an unsigned APK/AAB**, preceded by a
`::warning::no keystore secret set; APK will be unsigned` annotation. Set all four
secrets together.

### Generating a keystore locally

```bash
keytool -genkeypair -v \
  -keystore release.keystore.jks -alias dive \
  -keyalg RSA -keysize 2048 -validity 10000
```

Keep the file and both passwords safe — they are your update-signing identity; losing
them means you can never ship an update under the same application signature. Never
commit it: `.gitignore` blocks `*.jks`, `*.keystore` and `keystore.properties` on
purpose.

Base64-encode it for the `KEYSTORE_BASE64` secret:

```bash
# Git Bash / Linux
base64 -w0 release.keystore.jks > keystore.b64
```

```powershell
# PowerShell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("release.keystore.jks")) | Set-Content -NoNewline keystore.b64
```

## Desktop packaging (PyInstaller) and Desktop releases

From `desktop/backend`:

```bash
uv sync
uv run --with pyinstaller pyinstaller short-lab.spec --noconfirm
```

- Produces **`dist/short-lab/`**, an *onedir* bundle — start it with
  `dist/short-lab/short-lab.exe` (serves `127.0.0.1:46408` and opens the UI).
  Both console scripts (`short-lab` and the legacy `dive-desktop` alias) map to
  the same `diveintocrypto_desktop.__main__:main` entry.
- **Why not onefile:** single-exe self-extractors are a classic antivirus
  false-positive trigger and pay a temp-dir extraction cost on every launch. The onedir
  layout avoids both.
- The spec bundles the committed prebuilt UI (`desktop/ui/dist`) so the frozen app can
  serve it. It is placed twice on purpose: at `_internal/ui/dist` (`sys._MEIPASS/ui/dist`,
  used by the frozen-build fallback in `api/app.py`) and — via the packaging job's mirror
  step — at `<dist-root>/ui/dist`, which is where `api/app.py`'s original `_UI_DIST` path
  math (`parents[4]/ui/dist`) resolves inside a frozen tree. Extract/release the whole
  `dist/` tree together and both resolvers work. See the `short-lab.spec` header for the
  full path math.
- The spec also bundles the DuckDB native libraries and the full 005 resource set,
  all read via `diveintocrypto_desktop.resources.read_resource_text`
  (`importlib.resources` with a `_MEIPASS` fallback that is only covered by
  the frozen product smoke, never by unit-test fakes): `engine/config/default.yaml`,
  `shortlab/default.yaml` (overridable at runtime via `SHORTLAB_CONFIG_PATH`),
  `shortlab/identity/asset_overrides.yaml`, `shortlab/identity/verified_assets.yaml`
  and `shortlab/migrations/001_init.sql` … `005_hedge_advisor.sql` (F01 base
  001–004 plus the H01 Hedge advisor 005; H11 verifies the full manifest).
- **Writable data:** the frozen app never writes next to its resources. Short-Lab state
  lives in the per-user data directory (`%LOCALAPPDATA%/short-lab` on Windows, holding
  `shortlab.duckdb`), so launching from a read-only extraction directory works and data
  survives restarts. Smoke (`scripts/smoke_shortlab_packaged.py`): read-only install
  dir + empty user data dir, real-process `/api/health` + `/api/short/health`,
  migrate to schema 5, engine resources, plan registration with the public-HTTP
  fixture stub (never demo mode), clean shutdown, second boot recovering the
  plan/monitor/alerts. The UI bundle (`desktop/ui/dist/`) is rebuilt from the
  approved UI source by H11 and bound by source-commit + artifact SHA (node +
  esbuild versions recorded in the verification manifest).
- **Size honesty: expect roughly 150–250 MB unzipped.** pandas + numpy + FastAPI/uvicorn
  dominate; that is the price of shipping the full reference engine, not a packaging bug.
  The zip the packaging job uploads is smaller, but still comfortably in the
  three-digit-MB range.
- Dependencies come from the locked env — see
  [`desktop/backend/requirements-freeze.md`](../desktop/backend/requirements-freeze.md).

Desktop CI (`package-desktop` job in `release.yml`):

- Push a `short-lab-v*` tag → packages on `windows-latest`, creates the GitHub Release
  for the tag (Desktop-only, research build — not an automated trader) and attaches
  `short-lab-windows-x64.zip`.
- Manual runs: GitHub → **Actions → Release → Run workflow** → tick `package_desktop`.
  Uploads `short-lab-windows-x64.zip` as a workflow artifact only (no GitHub Release).
- A `short-lab-v*` tag never starts the Android job; a `v*` tag never starts the Desktop
  job — see the routing matrix in
  `desktop/backend/tests/test_shortlab_release_workflow.py`.
- Both upload steps (`short-lab-windows-x64` + `short-lab-verification-*`) run
  `if: always()` with `if-no-files-found: error`, so evidence is downloadable
  even when an earlier step failed. The verification artifact mirrors
  `desktop/backend/runtime/verification/<build-id>/` (manifest + logs + SHAs);
  CI run/artifact URLs are filled by real CI only, never invented locally.

Upgrading in the same Python environment: uninstall the old `diveintocrypto-desktop`
distribution first, then install `short-lab-desktop`. Do not install both side by side.

## This machine: `git push` over 1 MB is broken

On the owner's machine `git push` fails for transfers larger than ~1 MB, which most
release pushes are (a tag push carries any not-yet-pushed commits under it). Releases
must therefore be creatable **via the REST API or CI**, not a local push:

- Create the tag on GitHub without pushing:
  `POST /repos/{owner}/{repo}/git/refs` with
  `{"ref": "refs/tags/v0.x.y", "sha": "<commit sha already on GitHub>"}` — the `push`
  event fires and `release.yml` takes over. Same for Desktop tags with
  `{"ref": "refs/tags/short-lab-v0.x.y", ...}`.
- Or create tag + release in one call:
  `gh release create v0.x.y --target <branch-or-sha> --title ... --notes ...` creates
  the tag server-side; the workflow then attaches the built artefacts.

In both cases the commits themselves must have reached GitHub first via the API ship
path. The `release.yml` workflow creates the GitHub Release through the preinstalled
`gh` CLI (a GitHub REST API wrapper) — no local `git push` involved.
