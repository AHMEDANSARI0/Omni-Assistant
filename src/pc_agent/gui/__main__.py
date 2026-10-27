"""Allow ``python -m pc_agent.gui`` to start the desktop GUI."""

import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main())
