"""Regression guard for the `@aihub/app-sdk` resolution fix.

Generated apps import platform hooks (`useDataset`, `useAppQuery`, the AI-Toggle
hooks, …) from the `@aihub/app-sdk` package specifier. That package is made
resolvable by VENDORING the SDK into the template (`src/sdk/`) and aliasing the
specifier in BOTH places that resolve modules:
  - tsconfig.json `compilerOptions.paths`  → the type-checker (tsc)
  - vite.config.ts `resolve.alias`         → the bundler (vite build + deploy)

If any of the three goes missing, every dataset/SDK app fails to build with
"Cannot find module '@aihub/app-sdk'". This fast, dependency-free test pins all
three so the regression can't silently come back.
"""
from __future__ import annotations

import json
from pathlib import Path

_TEMPLATE = Path(__file__).resolve().parents[2] / "app-template"


def test_sdk_is_vendored_into_template():
    idx = _TEMPLATE / "src" / "sdk" / "index.ts"
    assert idx.exists(), "app-template/src/sdk/index.ts missing — SDK not vendored into the template"
    src = idx.read_text(encoding="utf-8")
    # The two exports the dataset prompt tells the AI to import most often.
    assert "useDataset" in src, "vendored SDK index is missing useDataset"
    assert "useAppQuery" in src, "vendored SDK index is missing useAppQuery"


def test_tsconfig_aliases_the_sdk():
    cfg = json.loads((_TEMPLATE / "tsconfig.json").read_text(encoding="utf-8"))
    paths = cfg.get("compilerOptions", {}).get("paths", {})
    assert "@aihub/app-sdk" in paths, (
        "app-template/tsconfig.json compilerOptions.paths must alias '@aihub/app-sdk' "
        "or tsc can't resolve the SDK imports"
    )
    target = " ".join(paths["@aihub/app-sdk"])
    assert "src/sdk" in target, f"@aihub/app-sdk tsconfig path should point at src/sdk, got {target!r}"


def test_vite_aliases_the_sdk():
    raw = (_TEMPLATE / "vite.config.ts").read_text(encoding="utf-8")
    assert "@aihub/app-sdk" in raw and "src/sdk" in raw, (
        "app-template/vite.config.ts must alias '@aihub/app-sdk' → src/sdk so the bundler "
        "(and deploy builds) resolve the SDK the same way tsc does"
    )
