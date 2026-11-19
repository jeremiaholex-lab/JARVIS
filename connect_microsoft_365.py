"""Connect a Microsoft account using device-code OAuth with read-only Graph scopes."""
from __future__ import annotations

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvis.skills.microsoft_365 import Microsoft365Connector

if __name__ == "__main__":
    Microsoft365Connector().connect()
