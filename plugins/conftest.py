"""Run a plugin's tests only when that plugin is installed (pip install -e plugins/<name>)."""
import importlib.util
from pathlib import Path

collect_ignore = []
for folder in Path(__file__).parent.iterdir():
    src = folder / "src"
    if src.is_dir():
        packages = [p.name for p in src.iterdir() if p.is_dir() and not p.name.endswith(".egg-info")]
        if not all(importlib.util.find_spec(name) for name in packages):
            collect_ignore.append(str(folder))
