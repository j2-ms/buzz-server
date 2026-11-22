class Error(Exception):
    """Only fixed, operator-safe messages may cross the CLI boundary."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)
