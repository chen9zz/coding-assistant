"""Actionable diagnostics for integrations that are intentionally optional."""

from __future__ import annotations


class OptionalDependencyError(RuntimeError):
    """Explain which project extra enables a requested integration."""

    def __init__(self, *, extra: str, dependency: str) -> None:
        self.extra = extra
        self.dependency = dependency
        self.install_command = f'python -m pip install "repopilot-r[{extra}]"'
        super().__init__(
            f"{dependency} is required for this integration; install it with: "
            f"{self.install_command}"
        )
