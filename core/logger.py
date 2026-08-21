# core/logger.py
import os
import csv
import json
import logging
import hashlib
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Any, Dict

def setup_system_logger(log_dir: Path, experiment_name: str) -> logging.Logger:
    """Configures console and rotating file logs for debugging and system status."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{experiment_name}_events.log"

    logger = logging.getLogger("VNASuite")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    # Avoid duplicate handlers on multiple initializations
    if logger.hasHandlers():
        logger.handlers.clear()

    # File handler (detailed)
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh_formatter = logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] [%(name)s:%(lineno)d]: %(message)s", 
                                     datefmt="%Y-%m-%d %H:%M:%S")
    fh.setFormatter(fh_formatter)

    # Console handler (scannable)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    ch.setFormatter(ch_formatter)

    logger.addHandler(fh)
    logger.addHandler(ch)

    # Mute external driver noise
    logging.getLogger("pynanovna").setLevel(logging.ERROR)
    logging.getLogger("pynanovna.hardware.Hardware").setLevel(logging.ERROR)

    return logger


class ExperimentTelemetryLogger:
    """Writes synchronized multi-instrument CSV logs and run manifests."""
    
    CSV_HEADERS = [
        "iso_time", "epoch_time", "touchstone_file", "touchstone_sha256",
        "ambient_temp_c", "rtd_setpoint_c", "rtd_measured_temp_c", "rtd_resistance_ohm",
        "rtd_current_a", "rtd_voltage_v", "rtd_phase", "dut_voltage_v", 
        "dut_current_ma", "dut_power_mw", "anomaly_score", "anomaly_flag"
    ]

    def __init__(self, output_dir: Path, experiment_name: str, config_snapshot: dict):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = self.output_dir / f"{experiment_name}_telemetry.csv"
        
        self._write_manifest(config_snapshot)
        self._init_csv()

    def _get_git_hash(self) -> str:
        try:
            return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"]).decode().strip()
        except Exception:
            return "unversioned"

    def _write_manifest(self, config: dict):
        manifest_path = self.output_dir / "run_manifest.json"
        metadata = {
            "created_at": datetime.now().isoformat(),
            "git_commit": self._get_git_hash(),
            "config": config
        }
        with open(manifest_path, "w") as f:
            json.dump(metadata, f, indent=2)

    def _init_csv(self):
        if not self.csv_path.exists():
            with open(self.csv_path, mode="w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(self.CSV_HEADERS)

    @staticmethod
    def compute_sha256(filepath: Path) -> str:
        """Generates SHA-256 for Touchstone file authentication."""
        sha = hashlib.sha256()
        with open(filepath, "rb") as f:
            while chunk := f.read(8192):
                sha.update(chunk)
        return sha.hexdigest()

    def log_sweep(self, row_data: Dict[str, Any]):
        """Appends one synchronized acquisition row to the CSV."""
        with open(self.csv_path, mode="a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.CSV_HEADERS)
            writer.writerow(row_data)