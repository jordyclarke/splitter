# PyInstaller spec — build on Windows (see .github/workflows/build-windows.yml)

block_cipher = None

from PyInstaller.utils.hooks import collect_dynamic_libs  # noqa: E402

pyzbar_binaries = collect_dynamic_libs("pyzbar")

a = Analysis(
    ["pod_splitter.py"],
    pathex=[],
    binaries=pyzbar_binaries,
    datas=[],
    hiddenimports=[
        "pyzbar",
        "pyzbar.pyzbar",
        "fitz",
        "watchdog",
        "watchdog.observers",
        "watchdog.observers.read_directory_changes",
        "PIL",
        "PIL.Image",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "torch", "scipy"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="POD_Splitter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="POD_Splitter",
)
