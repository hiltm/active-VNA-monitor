# run_z80_test.py
import sys
import time
import argparse
import dataclasses
from pathlib import Path
from datetime import datetime

from config import AppConfig
from core.models import MeasurementRecord
from core.storage import DataStorage
from core.logger import setup_system_logger, ExperimentTelemetryLogger
from core.ui_helpers import wait_for_user_with_flash
from drivers.vna_driver import VNADevice
from drivers.gpd2303s import GPD2303S
from drivers.dut_power import DUTPowerController
from drivers.temp_sensor import TemperatureSensor
from plugins.ml_engine import RFAgingClassifier
from targets.z80.z80_driver import Z80Controller


class Z80ExperimentRunner:
    STAGES = [
        "1_UNPOWERED",
        "2_POWERED_IDLE",
        "3_POWERED_Z80_ACTIVE",
        "4_POWERED_Z80_ACTIVE_COOLING"
    ]

    def __init__(self, cfg: AppConfig, serial_number: str, step_duration_s: float = 60.0):
        self.cfg = cfg
        self.serial_number = serial_number
        self.step_duration_s = step_duration_s

        # Folder convention: <serial_number>_<YYYYMMDD_HHMMSS>
        self.timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.experiment_name = f"{self.serial_number}_{self.timestamp_str}"
        self.experiment_dir = Path(cfg.base_save_dir) / self.experiment_name
        self.experiment_dir.mkdir(parents=True, exist_ok=True)

        self.logger = setup_system_logger(self.experiment_dir, self.experiment_name)
        
        cfg_snapshot = dataclasses.asdict(cfg)
        cfg_snapshot["serial_number"] = self.serial_number
        cfg_snapshot["run_timestamp"] = self.timestamp_str

        self.telemetry = ExperimentTelemetryLogger(
            output_dir=self.experiment_dir,
            experiment_name=self.experiment_name,
            config_snapshot=cfg_snapshot
        )
        self.storage = DataStorage(cfg.base_save_dir, self.experiment_name)

        # Hardware & Plugin Handles
        self.vna = None
        self.psu = None
        self.dut = None
        self.temp_sensor = None
        self.ml_classifier = None
        self.z80 = None

    def initialize_hardware(self):
        self.logger.info(f"Initializing 4-Stage Test for DUT: {self.serial_number}")
        self.logger.info(f"Destination: {self.experiment_dir}")

        # 1. NanoVNA
        self.vna = VNADevice(self.cfg.start_freq, self.cfg.stop_freq, self.cfg.points, self.cfg.vna_port)
        self.vna.connect()

        # 2. Power Supply
        self.psu = GPD2303S(self.cfg.power_supply_port)
        self.psu.connect()
        self.dut = DUTPowerController(self.psu, channel=self.cfg.dut_channel)
        self.dut.power_off()

        # 3. Ambient Sensor
        if self.cfg.enable_ambient_temp:
            self.temp_sensor = TemperatureSensor(self.cfg.ambient_temp_port)
            self.temp_sensor.connect()

        # 4. ML Classifier
        if self.cfg.enable_ml_evaluation:
            self.ml_classifier = RFAgingClassifier(self.cfg.ml_model_dir, self.cfg.ml_model_type)
            self.ml_classifier.load_model()

        # 5. Z80 Controller
        self.z80 = Z80Controller(
            port=self.cfg.z80_serial_port,
            baud_rate=self.cfg.z80_baud_rate,
            binary_path=self.cfg.z80_binary_path,
            binary_addr=self.cfg.z80_binary_addr,
            kickoff_byte=self.cfg.z80_kickoff_byte
        )

    def capture_single_sweep(self, stage_name: str) -> dict:
        t_dt = datetime.now()
        t_epoch = time.time()

        ambient_val = self.temp_sensor.read_temperature() if self.temp_sensor else None
        dut_stats = self.dut.read_telemetry() if self.dut else {}

        # Capture S-parameters
        freqs, s11, s21, s12, s22 = self.vna.sweep()

        record = MeasurementRecord(
            timestamp=t_dt,
            freqs=freqs,
            s11=s11,
            s21=s21,
            s12=s12,
            s22=s22,
            temperature=ambient_val,
            power_state=dut_stats
        )

        # Save to stage-specific subfolder (e.g., 1_UNPOWERED/measurement_...)
        s2p_path = self.storage.save_record(record, subfolder=stage_name)
        file_hash = ExperimentTelemetryLogger.compute_sha256(s2p_path)

        # ML Aging Classification
        ml_res = {
            'class': None,
            'label': 'Disabled',
            'confidence': None,
            'probabilities': [None, None, None]
        }
        if self.ml_classifier:
            ml_res = self.ml_classifier.evaluate(record)

        # Log relative path inside telemetry CSV
        relative_s2p_name = f"{stage_name}/{s2p_path.name}"
        self.telemetry.log_sweep({
            "iso_time": t_dt.isoformat(),
            "epoch_time": t_epoch,
            "touchstone_file": relative_s2p_name,
            "touchstone_sha256": file_hash,
            "ambient_temp_c": ambient_val,
            "rtd_setpoint_c": None,
            "rtd_measured_temp_c": None,
            "rtd_resistance_ohm": None,
            "rtd_current_a": None,
            "rtd_voltage_v": None,
            "rtd_phase": stage_name,
            "dut_voltage_v": dut_stats.get("dut_voltage_v", 0.0),
            "dut_current_ma": dut_stats.get("dut_current_a", 0.0) * 1000,
            "dut_power_mw": dut_stats.get("dut_power_w", 0.0) * 1000,
            "ml_class": ml_res['class'],
            "ml_label": ml_res['label'],
            "ml_confidence": ml_res['confidence'],
            "ml_prob_fresh": ml_res['probabilities'][0],
            "ml_prob_light_aged": ml_res['probabilities'][1],
            "ml_prob_heavy_aged": ml_res['probabilities'][2]
        })

        return ml_res

    def run_stage_for_duration(self, stage_name: str, duration_s: float):
        self.logger.info(f"=== ENTERING STAGE: {stage_name} (Duration: {duration_s:.0f}s) ===")
        start = time.monotonic()
        while (time.monotonic() - start) < duration_s:
            res = self.capture_single_sweep(stage_name)
            if res['class'] is not None:
                self.logger.info(
                    f"[{stage_name}] Class {res['class']} ({res['label']}) | "
                    f"Conf: {res['confidence']:.1%}"
                )
            time.sleep(self.cfg.loop_interval_seconds)

    def execute_sequence(self):
        self.initialize_hardware()

        try:
            # ----------------------------------------------------
            # STAGE 1: UNPOWERED
            # ----------------------------------------------------
            self.dut.power_off()
            self.run_stage_for_duration("1_UNPOWERED", self.step_duration_s)

            # ----------------------------------------------------
            # PROMPT 1 -> 2
            # ----------------------------------------------------
            wait_for_user_with_flash(f"[{self.serial_number}] Step 1 Complete -> Prepare for Step 2 (POWERED)")

            # ----------------------------------------------------
            # STAGE 2: POWERED (IDLE)
            # ----------------------------------------------------
            self.dut.power_on(voltage=self.cfg.dut_voltage, current_limit=self.cfg.dut_current_limit)
            time.sleep(1.0)
            self.run_stage_for_duration("2_POWERED_IDLE", self.step_duration_s)

            # ----------------------------------------------------
            # AUTO ADVANCE 2 -> 3: PROGRAM Z80 & START TRAFFIC
            # ----------------------------------------------------
            self.logger.info(f"[{self.serial_number}] Auto-transitioning to Step 3: Programming Z80 & Starting Software...")
            if self.z80.program_and_run():
                stage3_log = self.experiment_dir / "3_POWERED_Z80_ACTIVE" / f"{self.serial_number}_Z80_Responses.txt"
                self.z80.start_monitoring(stage3_log)
            else:
                self.logger.error("Z80 BLOAD programming failed! Continuing acquisition loop without active serial traffic...")

            # ----------------------------------------------------
            # STAGE 3: POWERED + Z80 ACTIVE
            # ----------------------------------------------------
            self.run_stage_for_duration("3_POWERED_Z80_ACTIVE", self.step_duration_s)

            # ----------------------------------------------------
            # PROMPT 3 -> 4
            # ----------------------------------------------------
            wait_for_user_with_flash(f"[{self.serial_number}] Step 3 Complete -> Enable Cooling for Step 4")

            # ----------------------------------------------------
            # STAGE 4: POWERED + Z80 ACTIVE + COOLING
            # ----------------------------------------------------
            # Point traffic logging to Stage 4 subfolder
            self.z80.stop_monitoring()
            stage4_log = self.experiment_dir / "4_POWERED_Z80_ACTIVE_COOLING" / f"{self.serial_number}_Z80_Responses.txt"
            self.z80.start_monitoring(stage4_log)

            self.run_stage_for_duration("4_POWERED_Z80_ACTIVE_COOLING", self.step_duration_s)

            self.logger.info(f">>> All 4 stages finished successfully for DUT {self.serial_number}. <<<")

        except KeyboardInterrupt:
            self.logger.warning("Test run aborted by operator.")
        finally:
            if self.z80:
                self.z80.disconnect()
            if self.dut:
                self.dut.power_off()
            if self.psu:
                self.psu.close()
            if self.ambient_sensor:
                self.ambient_sensor.close()
            if self.vna:
                self.vna.close()
            self.logger.info(f"Sequence complete. Outputs saved in {self.experiment_dir}")


def parse_args():
    parser = argparse.ArgumentParser(description="Z80 4-Stage RF & Thermal Test Sequence")
    parser.add_argument("--sn", type=str, required=True, help="DUT Serial Number (e.g. SN006, Z80_MOD3_01)")
    parser.add_argument("--duration", type=float, default=60.0, help="Duration (seconds) per stage (default: 60.0)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config = AppConfig()
    runner = Z80ExperimentRunner(config, serial_number=args.sn, step_duration_s=args.duration)
    runner.execute_sequence()