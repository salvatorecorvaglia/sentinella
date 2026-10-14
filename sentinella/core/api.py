"""Moved to :mod:`sentinella.server.api`; this alias keeps old imports working.

It *is* that module (not a copy), so ``patch("sentinella.core.api.…")``
patches the code that actually runs. Slated for removal in the next release.
"""

import sys

from sentinella.server import api as _moved

sys.modules[__name__] = _moved
