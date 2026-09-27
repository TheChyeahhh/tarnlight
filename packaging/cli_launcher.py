"""tarnlight-cli.exe: the full command line, the same as `python -m tarnlight`. With no command it opens the console."""
import sys

from tarnlight.__main__ import main

sys.exit(main())
