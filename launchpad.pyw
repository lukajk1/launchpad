# Double-click to open Launchpad (no console). Same as `python launchpad.py`.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import launchpad

launchpad.main()
