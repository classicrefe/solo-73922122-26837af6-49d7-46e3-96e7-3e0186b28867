"""Domain-level errors mapped to HTTP responses in ``main``."""

from __future__ import annotations


class AppError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class NotFoundError(AppError):
    def __init__(self, detail: str) -> None:
        super().__init__(404, detail)


class ConflictError(AppError):
    def __init__(self, detail: str) -> None:
        super().__init__(409, detail)


class InjectedFault(RuntimeError):
    """Only raised when the demo fault-injection switch is enabled."""

    def __init__(self) -> None:
        super().__init__("injected write failure")
