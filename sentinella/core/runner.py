"""Moved to :mod:`sentinella.server.runner`; this alias keeps old imports working.

It *is* that module (not a copy), so ``patch("sentinella.core.runner.…")``
patches the code that actually runs. Slated for removal in the next release.
"""

import sys

from sentinella.server import runner as _moved

sys.modules[__name__] = _moved
