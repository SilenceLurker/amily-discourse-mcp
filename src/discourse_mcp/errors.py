class AppError(Exception):
    """Safe, deliberately bounded error text for CLI and MCP clients."""


class RemoteError(AppError):
    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain
