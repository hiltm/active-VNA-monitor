# core/models.py
from dataclasses import dataclass, field
from datetime import datetime
import numpy as np

@dataclass
class MeasurementRecord:
    timestamp: datetime
    freqs: np.ndarray
    s11: np.ndarray
    s21: np.ndarray
    s12: np.ndarray = field(default_factory=lambda: np.array([]))
    s22: np.ndarray = field(default_factory=lambda: np.array([]))
    temperature: float | None = None
    power_state: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    @property
    def timestamp_str(self) -> str:
        return self.timestamp.strftime("%Y%m%d_%H%M%S")
