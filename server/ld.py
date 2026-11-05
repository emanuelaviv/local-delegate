"""local-delegate entry point.

Claude starts it as `py -3 -E -X utf8 ld.py serve` (see .mcp.json). From a terminal:
`py -3 ld.py status`, `py -3 ld.py setup`, `py -3 ld.py calibrate`, `py -3 ld.py --help`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from local_delegate.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
