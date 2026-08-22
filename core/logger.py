# core/logger.py
import csv
import json
import logging
import hashlib
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Any, Dict

class ResilientFileHandler(logging.FileHandler):
    def __init__(self, filename: Path, encoding: str = "utf-8"):
        self.base_path = Path(filename)
        self.current_path = self.base_path
        super().__init__(str(self._get_writable_path(self.base_path)), mode="a", encoding=encoding)

    def _get_writable_path(self, target_path: Path) -> Path:
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "a", encoding="utf-8"):
                pass
            return target_path
        except (PermissionError, OSError):
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fallback = target_path.parent / f"{target_path.stem}_fallback_{ts}{target_path.suffix}"
            print(f"[LOGGER WARNING] Cannot write to {target_path.name}. Redirecting to {fallback.name}")
            return fallback

    def emit(self, record: logging.LogRecord):
        try:
            super().emit(record)
        except (PermissionError, OSError, IOError):
            self.handleError(record)
            self.close()
            self.current_path = self._get_writable_path(self.base_path)
            self.baseFilename = str(self.current_path)
            self.stream = self._open()
            super().emit(record)


def setup_system_logger(log_dir: Path, experiment_name: str) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{experiment_name}_events.log"

    logger = logging.getLogger("VNASuite")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if logger.hasHandlers():
        logger.handlers.clear()

    fh = ResilientFileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] [%(name)s:%(lineno)d]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))

    logger.addHandler(fh)
    logger.addHandler(ch)

    logging.getLogger("pynanovna").setLevel(logging.ERROR)
    logging.getLogger("pynanovna.hardware.Hardware").setLevel(logging.ERROR)

    return logger


class ExperimentTelemetryLogger:
    CSV_HEADERS = [
        "iso_time", "epoch_time", "touchstone_file", "touchstone_sha256",
        "ambient_temp_c", "rtd_setpoint_c", "rtd_measured_temp_c", "rtd_resistance_ohm",
        "rtd_current_a", "rtd_voltage_v", "rtd_phase", "dut_voltage_v", 
        "dut_current_ma", "dut_power_mw",
        "ml_class", "ml_label", "ml_confidence",
        "ml_prob_fresh", "ml_prob_light_aged", "ml_prob_heavy_aged",
        "blockchain_tx"
    ]

    def __init__(self, output_dir: Path, experiment_name: str, config_snapshot: dict):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.experiment_name = experiment_name
        self.csv_path = self.output_dir / f"{experiment_name}_telemetry.csv"
        
        self._write_manifest(config_snapshot)
        self._init_csv(self.csv_path)

    def _write_manifest(self, config: dict):
        manifest_path = self.output_dir / "run_manifest.json"
        try:
            git_hash = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"]).decode().strip()
        except Exception:
            git_hash = "unversioned"

        metadata = {
            "created_at": datetime.now().isoformat(),
            "git_commit": git_hash,
            "config": config
        }
        try:
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)
        except (PermissionError, OSError):
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            with open(self.output_dir / f"run_manifest_fallback_{ts}.json", "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)

    def _init_csv(self, path: Path):
        if not path.exists():
            try:
                with open(path, mode="w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(self.CSV_HEADERS)
            except (PermissionError, OSError):
                self._rollover_csv()

    def _rollover_csv(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.csv_path = self.output_dir / f"{self.experiment_name}_telemetry_fallback_{ts}.csv"
        with open(self.csv_path, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(self.CSV_HEADERS)
        logging.getLogger("VNASuite").warning(f"Telemetry locked. Diverted to {self.csv_path.name}")

    @staticmethod
    def compute_sha256(filepath: Path) -> str:
        sha = hashlib.sha256()
        try:
            with open(filepath, "rb") as f:
                while chunk := f.read(8192):
                    sha.update(chunk)
            return sha.hexdigest()
        except Exception:
            return "HASH_ERROR"

    def log_sweep(self, row_data: Dict[str, Any]):
        for _ in range(2):
            try:
                with open(self.csv_path, mode="a", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=self.CSV_HEADERS)
                    writer.writerow(row_data)
                return
            except (PermissionError, OSError):
                self._rollover_csv()