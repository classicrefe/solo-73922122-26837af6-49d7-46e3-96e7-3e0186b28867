"""Application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: str = "data/warehouse.db"
    enable_fault_injection: bool = False
    seed_companies: tuple[tuple[str, str], ...] = (
        ("company_a", "公交一公司"),
        ("company_b", "公交二公司"),
    )

    @classmethod
    def from_env(cls) -> "Settings":
        path = os.environ.get("DATABASE_PATH", "data/warehouse.db")
        fault = os.environ.get("ENABLE_FAULT_INJECTION", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return cls(database_path=str(Path(path)), enable_fault_injection=fault)
