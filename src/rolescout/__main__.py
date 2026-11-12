"""Allows ``python -m rolescout``; the scheduled job is started this way."""

import sys

from rolescout.cli import main

sys.exit(main())
