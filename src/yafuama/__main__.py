"""Allow running with: python -m yafuama"""

import uvicorn

from .config import settings

uvicorn.run(
    "yafuama.main:app",
    host=settings.host,
    port=settings.port,
    reload=True,
)
