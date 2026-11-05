"""Entry file for single-process hosts (Pterodactyl / Bloom Host Python egg).

The panel runs ``python main.py``. This file makes the checked-out source and
the ``pip install --prefix .local`` dependencies importable, then starts the
API server and the Discord bot together (see rankrecovery/stack.py).
"""

from __future__ import annotations

import glob
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
for site in glob.glob(os.path.join(ROOT, ".local", "lib", "python3*", "site-packages")):
    if site not in sys.path:
        sys.path.insert(0, site)

from rankrecovery.stack import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
