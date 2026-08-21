# core/storage.py
import logging
from pathlib import Path
from datetime import datetime
import numpy as np
import skrf as rf
from core.models import MeasurementRecord

logger = logging.getLogger("VNASuite")

class DataStorage:
    def __init__(self, base_dir: Path, experiment_name: str):
        self.base_dir = Path(base_dir) / experiment_name
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save_record(self, record: MeasurementRecord) -> Path:
        """Saves Touchstone file with corrected S21/S12 port mapping."""
        N = len(record.freqs)
        s = np.zeros((N, 2, 2), dtype=complex)

        # Standard RF matrix indexing
        s[:, 0, 0] = record.s11  # S11 (Input reflection)
        s[:, 0, 1] = record.s12  # S12 (Reverse transmission - zeros)
        s[:, 1, 0] = record.s21  # S21 (Forward transmission - measured)
        s[:, 1, 1] = record.s22  # S22 (Output reflection - zeros)

        freq_obj = rf.Frequency.from_f(record.freqs, unit='Hz')
        ntwk = rf.Network(frequency=freq_obj, s=s, name="NanoVNA")

        base_filename = f"measurement_{record.timestamp_str}.s2p"
        target_path = self.base_dir / base_filename

        for attempt in range(3):
            try:
                ntwk.write_touchstone(str(target_path))
                return target_path
            except (PermissionError, OSError) as e:
                ts_alt = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                target_path = self.base_dir / f"measurement_{ts_alt}.s2p"
                logger.warning(f"Could not write {base_filename} ({e}). Retrying as {target_path.name}")

        raise IOError("Failed to write Touchstone file after multiple path redirects.")