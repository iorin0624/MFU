#!/usr/bin/env python3
"""テンプレート内の静的 url_for() endpoint と Flask の実在 endpoint を突合する。"""
from __future__ import annotations

import argparse
import difflib
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

URL_FOR_PATTERN = re.compile(r"url_for\(\s*(['\"])([^'\"]+)\1")

@dataclass(frozen=True)
class TemplateReference:
    template_path: Path
    line_no: int
    endpoint: str


def _load_app_module(repo_root: Path):
    app_init = repo_root / "__init__.py"
    if not app_init.exists():
        raise RuntimeError(f"app factory の読み込みに失敗: {app_init} が見つかりません")

    spec = importlib.util.spec_from_file_location(
        "app",
        app_init,
        submodule_search_locations=[str(repo_root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("app モジュール spec の作成に失敗しました")

    module = importlib.util.module_from_spec(spec)
    sys.modules["app"] = module
    spec.loader.exec_module(module)
    return module


def collect_registered_endpoints(repo_root: Path) -> set[str]:
    module = _load_app_module(repo_root)
    create_app = getattr(module, "create_app", None)
    if create_app is None:
        raise RuntimeError("create_app() が見つかりません")

    app = create_app()
    return {rule.endpoint for rule in app.url_map.iter_rules()}


def _iter_template_files(repo_root: Path, additional_dirs: Iterable[str]) -> list[Path]:
    if additional_dirs:
        candidates: set[Path] = set()
        for rel in additional_dirs:
            base = (repo_root / rel).resolve()
            if not base.exists() or not base.is_dir():
                continue
            candidates.update(base.rglob("*.html"))
        return sorted(candidates)

    return sorted(p for p in repo_root.rglob("*.html") if ".git" not in p.parts)


def collect_template_refs(repo_root: Path, additional_dirs: Iterable[str]) -> list[TemplateReference]:
    refs: list[TemplateReference] = []
    for file_path in _iter_template_files(repo_root, additional_dirs):
        rel = file_path.relative_to(repo_root)
        for idx, line in enumerate(file_path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
            for m in URL_FOR_PATTERN.finditer(line):
                endpoint = m.group(2).strip()
                if endpoint:
                    refs.append(TemplateReference(template_path=rel, line_no=idx, endpoint=endpoint))
    return refs


def main() -> int:
    parser = argparse.ArgumentParser(description="template url_for endpoint 整合チェック")
    parser.add_argument("--repo-root", default=".", help="リポジトリルート")
    parser.add_argument(
        "--template-dir",
        action="append",
        default=[],
        help="追加で走査するテンプレートディレクトリ（相対パス）",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    refs = collect_template_refs(repo_root, args.template_dir)

    try:
        endpoints = collect_registered_endpoints(repo_root)
    except Exception as exc:
        print(f"[ERROR] Flask app の初期化に失敗しました: {exc}", file=sys.stderr)
        return 2

    missing: list[TemplateReference] = [ref for ref in refs if ref.endpoint not in endpoints]

    print(f"checked templates: {len({ref.template_path for ref in refs})}")
    print(f"static url_for refs: {len(refs)}")
    print(f"registered endpoints: {len(endpoints)}")

    if not missing:
        print("OK: missing endpoint は 0 件です")
        return 0

    print("\nNG: テンプレートが参照している未登録 endpoint 一覧")
    for ref in missing:
        suggestions = difflib.get_close_matches(ref.endpoint, endpoints, n=3, cutoff=0.6)
        suggestion_text = f" (候補: {', '.join(suggestions)})" if suggestions else ""
        print(f"- {ref.template_path}:{ref.line_no} -> {ref.endpoint}{suggestion_text}")

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
