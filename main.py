"""Entry file for single-process hosts (Pterodactyl / Bloom Host Python egg).

The panel runs ``python main.py``. The bootstrap refuses Python older than
3.11 with a clear message, makes the checked-out source and the
``pip install --prefix .local`` packages of *this* interpreter importable,
installs ``requirements.txt`` itself if they are missing, and then starts
the API server and the Discord bot together (see rankrecovery/stack.py).
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from rankrecovery.bootstrap import bootstrap  # noqa: E402

bootstrap(__import__("pathlib").Path(ROOT))

from rankrecovery.stack import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
