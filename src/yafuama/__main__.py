"""Allow running with: python -m yafuama"""

import socket
import sys

import uvicorn

from .config import settings


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return False
        except OSError:
            return True


if _port_in_use(settings.host, settings.port):
    print(
        f"ERROR: port {settings.port} is already in use. "
        "Kill the existing server first.",
        file=sys.stderr,
    )
    sys.exit(1)

uvicorn.run(
    "yafuama.main:app",
    host=settings.host,
    port=settings.port,
    reload=True,
)
