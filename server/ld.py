"""local-delegate entry point.

Claude starts it as `ld.py serve`, with the command in the plugin's .mcp.json. From a terminal:
`python ld.py status`, `python ld.py setup`, `python ld.py calibrate`, `python ld.py --help`.

Python reads this whole file before running it, so it uses only syntax every Python understands:
an older Python then gets a clear message instead of a syntax error.
"""

import os
import sys

if sys.version_info < (3, 11):
    sys.stderr.write(
        "Local Delegate needs Python 3.11 or newer, but this is Python %s (%s). Install a newer "
        "Python, check that `python --version` shows it in a new terminal, then restart Claude.\n"
        % (".".join([str(n) for n in sys.version_info[:3]]), sys.executable))
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))


def _key(path):
    return os.path.normcase(os.path.abspath(path or os.curdir))


def _inside(path, roots):
    p = _key(path)
    return any(p == r or p.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


# The connector uses only Python's standard library. Folders that PYTHONPATH adds outside Python's
# own folders are dropped, so a module there can't stand in for a standard one.
_own = [_key(d) for d in {sys.prefix, sys.base_prefix, sys.exec_prefix}]
_drop = {_key(p) for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p and not _inside(p, _own)}
sys.path[:] = [HERE] + [p for p in sys.path if _key(p) not in _drop and _key(p) != _key(HERE)]

# Text for the terminal and for Claude's logs is UTF-8, whatever Windows' code page is.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        pass

from local_delegate.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
