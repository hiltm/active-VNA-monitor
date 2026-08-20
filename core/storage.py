# core/storage.py
import os
import csv
from pathlib import Path
import skrf as rf
import numpy as np
from core.models import MeasurementRecord

class DataStorage:
    def __init__(self, base_dir: Path, experiment_name: str):
        self.base_dir = Path(base_dir) / experiment_name
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = self.base_dir / "experiment_log.csv"
        self._init_csv()

    def _init_csv(self):
        if not self.csv_path.exists():
            with open(self.csv_path, mode='w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['Timestamp', 'Temperature_C', 'Touchstone_File'])

    def save_record(self, record: MeasurementRecord) -> Path:
        # Build 2-Port Network
        N = len(record.freqs)
        s = np.zeros((N, 2, 2), dtype=complex)
        s[:, 0, 0] = record.s11
        s[:, 0, 1] = record.s21
        s[:, 1, 0] = record.s12
        s[:, 1, 1] = record.s22

        freq_obj = rf.Frequency.from_f(record.freqs, unit='Hz')
        ntwk = rf.Network(frequency=freq_obj, s=s, name="NanoVNA")

        s2p_filename = f"measurement_{record.timestamp_str}.s2p"
        s2p_filepath = self.base_dir / s2p_filename
        ntwk.write_touchstone(str(s2p_filepath))

        # Append CSV entry
        with open(self.csv_path, mode='a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([record.timestamp_str, record.temperature, s2p_filename])

        return s2p_filepath
