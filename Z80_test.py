# Z80_test.py
import sys
import time
import argparse
import dataclasses
import importlib.util
from pathlib import Path
from datetime import datetime

# 1. Add project root directory to Python module search path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 2. Project subsystem imports
from config import AppConfig
from core.models import MeasurementRecord
from core.storage import DataStorage
from core.logger import setup_system_logger, ExperimentTelemetryLogger
from core.ui_helpers import wait_for_user_with_flash
from drivers.vna_driver import VNADevice
from drivers.gpd2303s import GPD2303S
from drivers.dut_power import DUTPowerController
from drivers.temp_sensor import TemperatureSensor
from ui.visualizer import LivePlotter
from plugins.ml_engine import RFAgingClassifier

# 3. Robust Z80 controller import with case-insensitivity & path fallback
try:
    from targets.z80.z80_driver import Z80Controller
except (ModuleNotFoundError, ImportError):
    try:
        from targets.Z80.Z80_driver import Z80Controller
    except (ModuleNotFoundError, ImportError):
        possible_paths = [
            PROJECT_ROOT / "targets" / "z80" / "z80_driver.py",
            PROJECT_ROOT / "targets" / "Z80" / "Z80_driver.py",
            PROJECT_ROOT / "targets" / "z80" / "Z80_driver.py",
            PROJECT_ROOT / "targets" / "Z80" / "z80_driver.py",
        ]
        driver_file = next((p for p in possible_paths if p.is_file()), None)
        if driver_file is None:
            raise FileNotFoundError(
                f"Could not locate 'z80_driver.py'. Checked:\n" +
                "\n".join(f" - {p}" for p in possible_paths)
            )
        spec = importlib.util.spec_from_file_location("z80_driver", str(driver_file))
        z80_mod = importlib.util.module_from_spec(spec)
        sys.modules["z80_driver"] = z80_mod
        spec.loader.exec_module(z80_mod)
        Z80Controller = z80_mod.Z80Controller


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

        # Unique folder naming: <serial_number>_<YYYYMMDD_HHMMSS>
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

        # Hardware, Plugin & GUI Handles
        self.vna = None
        self.psu = None
        self.dut = None
        self.temp_sensor = None
        self.ml_classifier = None
        self.plotter = None
        self.z80 = None

    def initialize_hardware(self):
        self.logger.info(f"Initializing 4-Stage Test for DUT: {self.serial_number}")
        self.logger.info(f"Destination: {self.experiment_dir}")

        # 1. NanoVNA (required — this is the core acquisition device)
        self.logger.info(f"Connecting to NanoVNA (Port: {self.cfg.vna_port})...")
        self.vna = VNADevice(self.cfg.start_freq, self.cfg.stop_freq, self.cfg.points, self.cfg.vna_port)
        self.vna.connect()

        # 2. Power Supply (optional — skip if not connected)
        try:
            self.logger.info(f"Connecting to PSU on {self.cfg.power_supply_port}...")
            self.psu = GPD2303S(self.cfg.power_supply_port)
            self.psu.connect()
            self.dut = DUTPowerController(self.psu, channel=self.cfg.dut_channel)
            self.dut.power_off()
        except Exception as e:
            self.logger.warning(f"PSU not connected ({e}). Continuing without power-supply control.")
            self.psu = None
            self.dut = None

        # 3. Ambient Sensor (optional)
        if self.cfg.enable_ambient_temp:
            try:
                self.logger.info(f"Connecting to ambient sensor on {self.cfg.ambient_temp_port}...")
                self.temp_sensor = TemperatureSensor(self.cfg.ambient_temp_port)
                self.temp_sensor.connect()
            except Exception as e:
                self.logger.warning(f"Ambient sensor not connected ({e}). Continuing without ambient temperature.")
                self.temp_sensor = None

        # 4. ML Aging Classifier
        if self.cfg.enable_ml_evaluation:
            self.logger.info(f"Loading {self.cfg.ml_model_type.upper()} classifier from {self.cfg.ml_model_dir}...")
            self.ml_classifier = RFAgingClassifier(self.cfg.ml_model_dir, self.cfg.ml_model_type)
            self.ml_classifier.load_model()

        # 5. Live Plotter GUI
        if self.cfg.enable_live_plot and not self.plotter:
            self.logger.info("Starting live visualizer GUI...")
            self.plotter = LivePlotter(self.cfg.start_freq, self.cfg.stop_freq)

        # 6. Z80 Target Driver (optional — construction only; connection verified at Stage 3)
        try:
            self.z80 = Z80Controller(
                port=self.cfg.z80_serial_port,
                baud_rate=self.cfg.z80_baud_rate,
                binary_path=self.cfg.z80_binary_path,
                binary_addr=self.cfg.z80_binary_addr,
                kickoff_byte=self.cfg.z80_kickoff_byte
            )
        except Exception as e:
            self.logger.warning(f"Z80 driver not initialized ({e}). Continuing without Z80 control.")
            self.z80 = None

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

        # Save into stage subfolder (e.g. 1_UNPOWERED/measurement_...)
        s2p_path = self.storage.save_record(record, subfolder=stage_name)
        file_hash = ExperimentTelemetryLogger.compute_sha256(s2p_path)

        # Real-time ML Aging Classification
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
            "dut_current_ma": (dut_stats.get("dut_current_a") or 0.0) * 1000,
            "dut_power_mw": (dut_stats.get("dut_power_w") or 0.0) * 1000,
            "ml_class": ml_res['class'],
            "ml_label": ml_res['label'],
            "ml_confidence": ml_res['confidence'],
            "ml_prob_fresh": ml_res['probabilities'][0],
            "ml_prob_light_aged": ml_res['probabilities'][1],
            "ml_prob_heavy_aged": ml_res['probabilities'][2]
        })

        # Update Live Plotter GUI
        if self.plotter:
            self.plotter.update(record)

        return ml_res

    def run_stage_for_duration(self, stage_name: str, duration_s: float):
        self.logger.info(f"=== ENTERING STAGE: {stage_name} (Duration: {duration_s:.0f}s) ===")
        start = time.monotonic()
        while (time.monotonic() - start) < duration_s:
            res = self.capture_single_sweep(stage_name)
            if res['class'] is not None and res['confidence'] is not None:
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
            if self.dut:
                self.dut.power_off()
            self.run_stage_for_duration("1_UNPOWERED", self.step_duration_s)

            # ----------------------------------------------------
            # PROMPT 1 -> 2
            # ----------------------------------------------------
            wait_for_user_with_flash(f"[{self.serial_number}] Step 1 Complete -> Prepare for Step 2 (POWERED)")

            # ----------------------------------------------------
            # STAGE 2: POWERED (IDLE)
            # NOTE: DUT power is applied MANUALLY for this board — the suite
            # does NOT drive the PSU output on. Ensure the DUT is powered
            # before the capture window begins.
            # ----------------------------------------------------
            self.run_stage_for_duration("2_POWERED_IDLE", self.step_duration_s)

            # ----------------------------------------------------
            # AUTO ADVANCE 2 -> 3: PROGRAM Z80 & START TRAFFIC
            # ----------------------------------------------------
            # STAGE 3 START NOTIFICATION
            self.logger.info("=" * 60)
            self.logger.info(f">>> STARTING STEP 3: POWERED + Z80 ACTIVE  (DUT {self.serial_number}) <<<")
            self.logger.info("=" * 60)
            print(
                f"\n\033[96;1m========================================================\033[0m\n"
                f"\033[92;1m>>> STEP 3 STARTING: POWERED + Z80 ACTIVE (DUT {self.serial_number}) <<<\033[0m\n"
                f"\033[96;1m========================================================\033[0m\n"
            )

            # Confirm Z80 serial comms before capturing Stage 3 data.
            # The operator presses the RESET button on the Z80 board and
            # confirms that the serial traffic after reset is valid — a working
            # DUT emits the expected 01 01 01 stream (see
            # Z80Controller.check_serial_comms / VALID_COMMS_SIGNAL in
            # Z80_driver.py). Then they press ENTER to proceed.
            if self.z80:
                wait_for_user_with_flash(
                    f"[{self.serial_number}] Press the Z80 RESET button and confirm valid serial "
                    f"traffic (expected 01 01 01). Press ENTER only once traffic looks good"
                )
                self.logger.info("Operator confirmed Z80 serial comms. Proceeding with Step 3 data capture.")

                self.logger.info(f"[{self.serial_number}] Programming Z80 & Starting Software...")
                if self.z80.program_and_run():
                    stage3_log = self.experiment_dir / "3_POWERED_Z80_ACTIVE" / f"{self.serial_number}_Z80_Responses.txt"
                    self.z80.start_monitoring(stage3_log)
                else:
                    self.logger.error("Z80 BLOAD programming failed! Continuing acquisition loop without active serial traffic...")
            else:
                self.logger.warning("No Z80 driver available; running Step 3 without Z80 traffic.")

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
            if self.z80:
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
            if self.temp_sensor:
                self.temp_sensor.close()
            if self.vna:
                self.vna.close()
            if self.plotter:
                self.plotter.close()
            self.logger.info(f"Sequence complete. Outputs saved in {self.experiment_dir}")


def parse_args():
    parser = argparse.ArgumentParser(description="Z80 4-Stage RF & Thermal Test Sequence")
    parser.add_argument("--sn", type=str, required=True, help="DUT Serial Number (e.g. 001, SN006, Z80_MOD3_01)")
    parser.add_argument("--duration", type=float, default=60.0, help="Duration (seconds) per stage (default: 60.0)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config = AppConfig()
    runner = Z80ExperimentRunner(config, serial_number=args.sn, step_duration_s=args.duration)
    runner.execute_sequence()