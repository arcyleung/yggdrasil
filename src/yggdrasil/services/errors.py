"""Service/MCP error types with stable codes."""
class YggdrasilError(Exception):
    code: str = "error"
    def __init__(self, message: str = "") -> None:
        self.message = message or self.code
        super().__init__(self.message)

class NotFoundError(YggdrasilError):
    code = "not_found"

class TrajectoryClosedError(YggdrasilError):
    code = "trajectory_closed"

class ValidationError(YggdrasilError):
    code = "validation_error"

class EmbedFailedError(YggdrasilError):
    code = "embed_failed"

class IndexFailedError(YggdrasilError):
    code = "index_failed"

class StoreFailedError(YggdrasilError):
    code = "store_failed"

class InvalidQueryError(YggdrasilError):
    code = "invalid_query"
