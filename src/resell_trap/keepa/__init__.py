class KeepaApiError(Exception):
    """Raised when a Keepa API call fails."""

    def __init__(self, message: str, tokens_left: int | None = None):
        super().__init__(message)
        self.tokens_left = tokens_left
