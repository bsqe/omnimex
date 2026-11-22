# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for Omnimex.

Builds ONE shared ``--onedir`` bundle (``dist/Omnimex/``) containing two executables
that share the same ``_internal/`` runtime folder:

1. ``Omnimex.exe``      (windowed desktop tray + embedded web UI server)
2. ``omnimex-cli.exe``  (console CLI + stdio MCP server for coding agents)
"""

from __future__ import annotations

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent
SRC_DIR = ROOT / "src"
FRONTEND_DIST = ROOT / "frontend" / "dist"
SKILLS_DIR = ROOT / "skills"
DEFAULT_CONFIG = ROOT / "omnimex.toml"
ICON_PATH = ROOT / "data" / "omnimex.ico"
FASTEMBED_CACHE = ROOT / "data" / "fastembed_cache"

if not (FRONTEND_DIST / "index.html").is_file():
    raise FileNotFoundError(
        f"Compiled frontend not found at {FRONTEND_DIST}. Run 'npm --prefix frontend run build' first."
    )
if not ICON_PATH.is_file():
    raise FileNotFoundError(
        f"Tray icon not found at {ICON_PATH}. Generate it with omnimex.tray.ensure_tray_ico()."
    )
if not FASTEMBED_CACHE.is_dir():
    raise FileNotFoundError(
        f"Bundled FastEmbed cache not found at {FASTEMBED_CACHE}. "
        "Populate it before building offline packages."
    )

datas: list[tuple[str, str]] = [
    (str(FRONTEND_DIST), "frontend/dist"),
    (str(SKILLS_DIR), "skills"),
    (str(DEFAULT_CONFIG), "."),
    (str(ICON_PATH), "data"),
    (str(FASTEMBED_CACHE), "data/fastembed_cache"),
]
binaries: list[tuple[str, str]] = []
hiddenimports: list[str] = [
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.IcoImagePlugin",
    "PIL.PngImagePlugin",
    "sqlite3",
    "_sqlite3",
    "tomllib",
    "mmh3",
    "loguru",
    "tqdm",
    "requests",
    "numpy",
    "httpx2",
    "cyclopts",
    "uncalled_for",
    "openapi_pydantic",
    "jsonschema_path",
    "watchfiles",
    "websockets",
]

for pkg in ("onnxruntime", "fastembed", "tokenizers", "py_rust_stemmers", "fastmcp", "mcp"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

for mod in (
    "omnimex",
    "uvicorn",
    "fastapi",
    "starlette",
    "anyio",
    "pydantic",
    "pydantic_core",
    "pystray",
    "watchdog",
    "key_value",
    "huggingface_hub",
    "httpx",
    "typer",
    "rich",
):
    hiddenimports += collect_submodules(mod)

for dist_name in (
    "omnimex",
    "fastmcp",
    "fastmcp-slim",
    "mcp",
    "mcp-types",
    "fastembed",
    "onnxruntime",
    "tokenizers",
    "py-rust-stemmers",
    "huggingface-hub",
    "uvicorn",
    "fastapi",
    "starlette",
    "pydantic",
    "typer",
    "rich",
    "numpy",
    "tqdm",
    "requests",
    "httpx",
    "pillow",
    "pystray",
    "watchdog",
    "py-key-value-aio",
):
    try:
        datas += copy_metadata(dist_name)
    except Exception:
        pass

hiddenimports = sorted(set(hiddenimports))
excludes = ["pytest", "_pytest", "ruff", "mypy", "tkinter", "_tkinter"]


def _dedup_toc(entries):
    seen: set[tuple[str, str]] = set()
    unique = []
    for entry in entries:
        dest, _src, kind = entry
        key = (dest.replace("\\", "/").lower(), str(kind))
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return unique


a_tray = Analysis(
    [str(ROOT / "packaging" / "entry_tray.py")],
    pathex=[str(SRC_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz_tray = PYZ(a_tray.pure)
exe_tray = EXE(
    pyz_tray,
    a_tray.scripts,
    [],
    exclude_binaries=True,
    name="Omnimex",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(ICON_PATH)],
)

a_cli = Analysis(
    [str(ROOT / "packaging" / "entry_cli.py")],
    pathex=[str(SRC_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz_cli = PYZ(a_cli.pure)
exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name="omnimex-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(ICON_PATH)],
)

coll = COLLECT(
    exe_tray,
    exe_cli,
    _dedup_toc(list(a_tray.binaries) + list(a_cli.binaries)),
    _dedup_toc(list(a_tray.datas) + list(a_cli.datas)),
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Omnimex",
)
