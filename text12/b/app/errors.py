class LeaseError(Exception):
    """Domain error that maps to a structured JSON response.

    code   - stable machine-readable error code
    status - HTTP status to return
    """

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
