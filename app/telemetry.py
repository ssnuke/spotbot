from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional


@dataclass
class Telemetry:
    enabled: bool = True
    logger: Optional[Callable[[str], None]] = print
    entries: List[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        if not self.enabled:
            return

        formatted = f"[{datetime.utcnow().isoformat()}] {message}"
        self.entries.append(formatted)
        if self.logger:
            self.logger(formatted)
