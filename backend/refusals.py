"""A refusal: a status and one line that never repeats what was sent."""


class Refusal(Exception):
    def __init__(self, status: int, line: str):
        super().__init__(line)
        self.status = status
        self.line = line
