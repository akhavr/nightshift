"""Allow running as `python -m host.watchdog`."""

import sys

from host.watchdog.main import main

if __name__ == "__main__":
    sys.exit(main())
