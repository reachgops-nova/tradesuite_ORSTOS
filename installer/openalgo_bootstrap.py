"""
==========================================================
TradeSuite
installer/openalgo_bootstrap.py
==========================================================

Fetches and configures OpenAlgo as its own separate component -- NOT
bundled/embedded inside TradeSuite's own source, NOT the customized fork
Gopinath runs for his own research (see the plan: initially thought to
be a substantial fork, but `git remote -v` / `git rev-list
origin/main...HEAD` on that install confirmed 2026-08-19 it's actually
unmodified upstream, 0 commits of divergence -- still fetched fresh here
rather than reused, since each customer needs their OWN instance with
their OWN broker credentials and database; see the plan's
one-instance-per-user-per-broker finding).

Pinned to a specific commit (not a moving `main` branch) so every
customer gets the exact version this product was built and tested
against, not whatever upstream happens to contain on their install day.

REWRITTEN 2026-08-24 to be genuinely standalone: the original version
required `git` and a system Python on the customer's machine, which
directly contradicted "must be a standalone exe -- customer runs it, it
takes care of all prerequisites" (Gopinath's explicit instruction). Now:
  - OpenAlgo's source comes from GitHub's plain HTTPS archive-zip
    endpoint (codeload.github.com), not `git clone` -- no git needed.
  - OpenAlgo's own Python runtime is a private, downloaded copy of
    python.org's official "embeddable" Windows distribution, bootstrapped
    with pip via get-pip.py -- no system Python needed. Deliberately kept
    separate from TradeSuite's own frozen interpreter (which is sealed
    inside the PyInstaller exe and can't run arbitrary pip installs).

Confirmed upstream source: https://github.com/marketcalls/openalgo.git
Confirmed the frontend ships pre-built in the repo (frontend/dist/ is
committed via an upstream CI job) -- customers do not need Node.js/npm.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

OPENALGO_OWNER = "marketcalls"
OPENALGO_REPO = "openalgo"
# Full 40-char SHA on purpose, not an abbreviated one -- unambiguous
# regardless of this repo's default short-hash length. Verified 2026-08-19
# as the exact revision already validated against ORB/Tamil in this
# product's own testing. Bump deliberately, after re-testing, not casually.
OPENALGO_PINNED_COMMIT = "20c5d5b0b3c00bf00ffcdcbbce50834e505d016e"
OPENALGO_ZIP_URL = f"https://github.com/{OPENALGO_OWNER}/{OPENALGO_REPO}/archive/{OPENALGO_PINNED_COMMIT}.zip"

# Private Python runtime for OpenAlgo -- python.org's official embeddable
# Windows build. Pinned for the same reproducibility reason as the OpenAlgo
# commit above.
#
# 3.11.9 was the original choice here, picked without actually checking
# requirements.txt against it -- wrong: live testing 2026-08-24 (Gopinath
# running the real bootstrap flow) failed on ipython==9.12.0, which
# requires Python >=3.12. Confirmed by grepping requirements.txt directly
# rather than guessing again: ipython==9.12.0 is the only hard >=3.12
# pin. Bumped to 3.12.10 (latest confirmed-published embeddable build at
# the time of this fix, verified the zip actually exists at this URL
# before pinning it -- don't repeat the "picked a plausible-looking
# version without checking" mistake).
PYTHON_VERSION = "3.12.10"
PYTHON_EMBED_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"

DOWNLOAD_TIMEOUT = 120


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)


def _download(url: str, dest: Path, log_fn) -> None:
    log_fn(f"Downloading {url.rsplit('/', 1)[-1]} ...")
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def _runtime_python_dir(runtime_dir: Path) -> Path:
    return runtime_dir.parent / "python_runtime"


def is_bootstrapped(runtime_dir: Path) -> bool:
    py = _runtime_python_dir(runtime_dir) / "python.exe"
    return (runtime_dir / "app.py").exists() and py.exists()


# ------------------------------------------------------------
# OpenAlgo source -- HTTPS zip, no git
# ------------------------------------------------------------

def fetch_source(runtime_dir: Path, log_fn=print) -> tuple[bool, str]:
    if runtime_dir.exists() and any(runtime_dir.iterdir()):
        return False, f"{runtime_dir} already exists and is not empty -- refusing to overwrite it."
    runtime_dir.parent.mkdir(parents=True, exist_ok=True)

    zip_path = runtime_dir.parent / "openalgo_source.zip"
    try:
        _download(OPENALGO_ZIP_URL, zip_path, log_fn)
    except Exception as ex:
        return False, f"Download failed: {ex}"

    log_fn("Extracting OpenAlgo source ...")
    extract_root = runtime_dir.parent / "_openalgo_extract_tmp"
    if extract_root.exists():
        shutil.rmtree(extract_root)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_root)
    zip_path.unlink(missing_ok=True)

    # GitHub's archive zip wraps everything in a single "<repo>-<sha>/" folder
    inner_dirs = list(extract_root.iterdir())
    if len(inner_dirs) != 1 or not inner_dirs[0].is_dir():
        return False, f"Unexpected archive layout: {[p.name for p in inner_dirs]}"
    inner_dirs[0].rename(runtime_dir)
    extract_root.rmdir()

    if not (runtime_dir / "app.py").exists():
        return False, "Extracted archive doesn't contain app.py -- pinned commit or repo layout may have changed."
    return True, "OpenAlgo source fetched and pinned."


# ------------------------------------------------------------
# Private Python runtime -- embeddable distribution + pip, no system Python
# ------------------------------------------------------------

def setup_python_runtime(runtime_dir: Path, log_fn=print) -> tuple[bool, str]:
    py_dir = _runtime_python_dir(runtime_dir)
    py_exe = py_dir / "python.exe"
    if py_exe.exists():
        return True, "Python runtime already set up."

    py_dir.mkdir(parents=True, exist_ok=True)
    zip_path = py_dir.parent / "python_embed.zip"
    try:
        _download(PYTHON_EMBED_URL, zip_path, log_fn)
    except Exception as ex:
        return False, f"Python runtime download failed: {ex}"

    log_fn("Extracting Python runtime ...")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(py_dir)
    zip_path.unlink(missing_ok=True)

    # The embeddable distribution ships with a "._pth" file that, by
    # default, EXCLUDES site-packages and disables `import site` -- pip
    # can't install anything until that's reversed. This is the standard,
    # documented way to un-restrict it (see python.org's embeddable-package
    # notes): uncomment the "import site" line and make sure Lib/site-packages
    # is on the search path.
    pth_files = list(py_dir.glob("python*._pth"))
    if not pth_files:
        return False, "Could not find the embeddable distribution's ._pth file."
    pth_file = pth_files[0]
    text = pth_file.read_text(encoding="utf-8")
    text = text.replace("#import site", "import site")
    if "Lib\\site-packages" not in text and "Lib/site-packages" not in text:
        text += "\nLib\\site-packages\n"
    # A ._pth file's entries are resolved relative to the ._pth file's OWN
    # directory (python_runtime\), never the script being run -- and its
    # mere presence puts the interpreter in isolated mode, which silently
    # ignores PYTHONPATH too (confirmed 2026-08-24 by hitting this for
    # real: OpenAlgo's app.py failed with "ModuleNotFoundError: No module
    # named 'utils'" even though utils/ genuinely existed right next to
    # app.py -- it just was never on sys.path). Only a real fix: add
    # OpenAlgo's own directory as an explicit absolute entry here.
    openalgo_path = str(runtime_dir.resolve())
    if openalgo_path not in text:
        text += f"\n{openalgo_path}\n"
    pth_file.write_text(text, encoding="utf-8")

    log_fn("Bootstrapping pip ...")
    get_pip_path = py_dir / "get-pip.py"
    try:
        _download(GET_PIP_URL, get_pip_path, log_fn)
    except Exception as ex:
        return False, f"get-pip.py download failed: {ex}"

    result = _run([str(py_exe), str(get_pip_path), "--no-warn-script-location"])
    get_pip_path.unlink(missing_ok=True)
    if result.returncode != 0:
        return False, f"pip bootstrap failed: {result.stderr}"

    # get-pip.py (modern versions) installs pip alone -- setuptools/wheel are
    # NOT bundled automatically the way a normal venv would include them.
    # Confirmed 2026-08-24 by hitting it for real: several of OpenAlgo's
    # requirements only ship as sdists (source distributions), and building
    # those needs setuptools' build backend -- pip fails with
    # "BackendUnavailable: Cannot import 'setuptools.build_meta'" without
    # this. Standard, documented fix for embeddable-Python + modern pip.
    log_fn("Installing setuptools/wheel (needed to build a few sdist-only packages) ...")
    result = _run([str(py_exe), "-m", "pip", "install", "-q", "setuptools", "wheel"])
    if result.returncode != 0:
        return False, f"setuptools/wheel install failed: {result.stderr}"

    return True, f"Private Python {PYTHON_VERSION} runtime ready."


def install_dependencies(runtime_dir: Path, log_fn=print) -> tuple[bool, str]:
    py_exe = _runtime_python_dir(runtime_dir) / "python.exe"
    log_fn("Installing OpenAlgo's dependencies (first run only, can take a few minutes) ...")
    result = _run([str(py_exe), "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=runtime_dir)
    if result.returncode != 0:
        return False, f"pip install failed: {result.stderr[-2000:]}"
    return True, "Dependencies installed."


# ------------------------------------------------------------
# .env configuration (unchanged from the original version)
# ------------------------------------------------------------

DEFAULT_BROKER = "flattrade"  # the broker this whole product is built around; see TradeSuite's plan


def configure_env(runtime_dir: Path, host_ip: str, port: int, log_fn=print, broker: str = DEFAULT_BROKER,
                   broker_api_key: str | None = None, broker_api_secret: str | None = None) -> tuple[bool, str]:
    sample = runtime_dir / ".sample.env"
    env_path = runtime_dir / ".env"
    if not sample.exists():
        return False, f"{sample} not found -- fetch may be incomplete."
    if not env_path.exists():
        env_path.write_text(sample.read_text(encoding="utf-8"), encoding="utf-8")

    text = env_path.read_text(encoding="utf-8")

    def _set(key: str, value: str, body: str) -> str:
        pattern = rf"^{re.escape(key)}\s*=.*$"
        replacement = f"{key} = '{value}'"
        if re.search(pattern, body, flags=re.MULTILINE):
            return re.sub(pattern, replacement, body, count=1, flags=re.MULTILINE)
        return body + f"\n{replacement}\n"

    text = _set("HOST_SERVER", f"http://{host_ip}:{port}", text)
    text = _set("FLASK_HOST_IP", host_ip, text)
    text = _set("FLASK_PORT", str(port), text)
    # OpenAlgo's own startup check (utils/env_check.py) hard-refuses to
    # launch with the literal "<broker>" placeholder or any name not in
    # VALID_BROKERS -- confirmed 2026-08-24 by hitting "Error: Invalid
    # REDIRECT_URL configuration detected" for real. The broker is baked
    # into REDIRECT_URL at startup, not chosen later through /broker's web
    # UI as originally assumed -- that page completes the OAuth/TOTP login
    # for whichever broker is already configured here, it doesn't pick one.
    text = _set("REDIRECT_URL", f"http://{host_ip}:{port}/{broker}/callback", text)
    text = _set("FLASK_DEBUG", "False", text)  # must never be True in a shipped product -- see .sample.env's own warning
    if broker_api_key:
        text = _set("BROKER_API_KEY", broker_api_key, text)
    if broker_api_secret:
        text = _set("BROKER_API_SECRET", broker_api_secret, text)

    env_path.write_text(text, encoding="utf-8")
    log_fn(f"Configured {env_path} for {host_ip}:{port}, broker={broker}. "
           "Secrets (APP_KEY/API_KEY_PEPPER/FERNET_SALT) stay as placeholders -- "
           "OpenAlgo auto-rotates them to real random values on its own first run.")
    return True, "Configured."


# ------------------------------------------------------------
# Orchestration
# ------------------------------------------------------------

def bootstrap(runtime_dir: Path, host_ip: str, port: int, log_fn=print, broker: str = DEFAULT_BROKER,
              broker_api_key: str | None = None, broker_api_secret: str | None = None) -> tuple[bool, str]:
    """Full first-time setup. Idempotent: safe to call again if a prior
    step failed partway (e.g. network dropped mid pip-install). Needs
    nothing pre-installed on the customer's machine beyond internet
    access -- no git, no system Python.

    broker_api_key/broker_api_secret are REQUIRED for OpenAlgo to actually
    start (not optional convenience) -- see configure_env()'s docstring
    note. The caller (engine/openalgo_manager.py) is responsible for not
    calling this until ConfigStore.is_broker_credentials_set() is True."""
    if not (runtime_dir / "app.py").exists():
        ok, msg = fetch_source(runtime_dir, log_fn)
        if not ok:
            return False, msg
        log_fn(msg)

    ok, msg = setup_python_runtime(runtime_dir, log_fn)
    if not ok:
        return False, msg
    log_fn(msg)

    ok, msg = configure_env(runtime_dir, host_ip, port, log_fn, broker=broker,
                             broker_api_key=broker_api_key, broker_api_secret=broker_api_secret)
    if not ok:
        return False, msg

    ok, msg = install_dependencies(runtime_dir, log_fn)
    if not ok:
        return False, msg

    return True, "OpenAlgo is set up and ready to launch."


def launch(runtime_dir: Path) -> subprocess.Popen:
    """Starts OpenAlgo as its own subprocess, using its own private Python
    runtime -- a separate interpreter/process from TradeSuite itself, on
    purpose (keeps it a genuinely arms-length component, not something
    statically linked into TradeSuite's own binary)."""
    py_exe = _runtime_python_dir(runtime_dir) / "python.exe"
    return subprocess.Popen(
        [str(py_exe), "app.py"], cwd=runtime_dir,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
