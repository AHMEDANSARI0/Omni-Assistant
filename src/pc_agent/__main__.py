"""Allow `python -m pc_agent` to start the CLI."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
