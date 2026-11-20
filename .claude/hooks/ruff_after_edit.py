"""PostToolUse: ruff format + ruff check --fix для только что изменённого .py-файла.

ruff ищется так: PATH → .venv проекта → .venv основного checkout (для worktree) → uvx.
Если ruff не найден, хук молча пропускает файл. Оставшиеся ошибки линтера возвращаются
Claude (exit 2), чтобы он их исправил.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


def git_roots(start: Path) -> list[Path]:
    """Корень текущего checkout и (для worktree) корень основного checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel", "--git-common-dir"],
            cwd=start,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.split("\n")
    except (OSError, subprocess.SubprocessError):
        return []
    if len(out) < 2 or not out[0]:
        return []
    top = Path(out[0])
    common = (start / out[1]).resolve()
    return [top, common.parent]


def find_ruff(roots: list[Path]) -> list[str] | None:
    if exe := shutil.which("ruff"):
        return [exe]
    for root in roots:
        for rel in (".venv/Scripts/ruff.exe", ".venv/bin/ruff"):
            if (root / rel).is_file():
                return [str(root / rel)]
    if exe := shutil.which("uvx"):
        return [exe, "ruff"]
    return None


def main() -> int:
    data = json.load(sys.stdin)
    path = Path((data.get("tool_input") or {}).get("file_path") or "")
    if path.suffix != ".py" or not path.is_file():
        return 0
    roots = git_roots(path.parent)
    ruff = find_ruff(roots)
    if ruff is None:
        return 0
    cwd = roots[0] if roots else path.parent

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*ruff, *args, str(path)],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

    res = run("check", "--fix", "--quiet")
    run("format", "--quiet")
    if res.returncode != 0:
        sys.stderr.reconfigure(encoding="utf-8")
        print(f"ruff нашёл ошибки в {path}:\n{res.stdout}{res.stderr}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
