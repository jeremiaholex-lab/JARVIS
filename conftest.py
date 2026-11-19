"""Make both the JARVIS package and the vendored OpenJarvis runtime importable
under a plain `pytest` run, so tests do not depend on which interpreter
launches them."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _path in (ROOT, ROOT / "openjarvis_runtime" / "src", ROOT / "orb"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
