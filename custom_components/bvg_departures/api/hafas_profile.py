"""BVG HAFAS mgate profile.

Copied verbatim from hafas-client `p/bvg/base.js` and `p/bvg/products.js` at
commit 5fe8fbd35e1bbcdd5478e425e639fa07668b71eb
(https://github.com/public-transport/hafas-client). Update from there when the
endpoint starts rejecting requests; do not guess values.
"""

from __future__ import annotations

from typing import Any, Final

HAFAS_CLIENT_COMMIT: Final = "5fe8fbd35e1bbcdd5478e425e639fa07668b71eb"

ENDPOINT: Final = "https://bvg.hafas.cloud/apps/gate"
AUTH: Final[dict[str, Any]] = {"type": "AID", "aid": "dVg4TZbW8anjx9ztPwe2uk4LVRi9wO"}
CLIENT: Final[dict[str, Any]] = {"type": "WEB", "id": "VBB", "v": 10002, "name": "webapp"}
EXT: Final = "BVG.1"
VER: Final = "1.72"
LANG: Final = "de"
TIMEZONE: Final = "Europe/Berlin"

# product id -> HAFAS class bitmask
PRODUCT_BITMASKS: Final[dict[str, int]] = {
    "suburban": 1,
    "subway": 2,
    "tram": 4,
    "bus": 8,
    "ferry": 16,
    "express": 32,
    "regional": 64,
}
