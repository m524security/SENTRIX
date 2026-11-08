"""
Pytest bootstrap.

Why this file exists
--------------------
`tests/` has no `__init__.py`, so pytest's default "prepend" import mode puts
`tests/` on sys.path — NOT the repo root. That means `from agent import config`
resolves only when the repo root happens to be on sys.path already:

    python -m pytest tests/    # works  — `python -m` prepends CWD
    pytest tests/              # FAILS  — the console script does not

CI runs the second form, which is how a green local suite still broke the build
with `ModuleNotFoundError: No module named 'agent'`.

pytest automatically inserts the directory containing the rootdir conftest.py
into sys.path, so simply existing here fixes it for every invocation style.
The explicit insert below is belt-and-braces for direct/IDE runners.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
