"""A source-capacity rejection is a normal design outcome, never a model error."""


class SeedRejected(ValueError):
    def __init__(self, report: dict):
        self.report = report
        super().__init__(report['reason'])


class AuthoringStalled(RuntimeError):
    """Retain the unfinished draft and stop automatic authoring retries."""

    def __init__(self, report: dict):
        self.report = report
        super().__init__(report['reason'])
