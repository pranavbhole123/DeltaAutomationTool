"""Double-click/run this file for the GUI; arguments select CLI commands."""
import sys
from bt_delta.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["gui"]))
