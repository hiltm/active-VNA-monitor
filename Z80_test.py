# Z80_test.py
import sys
import time
import argparse
import dataclasses
import importlib.util
from pathlib import Path
from datetime import datetime

# 1. Add project root directory to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 2. Subsystem imports
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

# 3. Dynamic Z80 driver loader with path fallback
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

        # Folder convention: <serial_number>_<YYYYMMDD_HHMMSS>
        self.timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.experiment_name = f"{self.serial_number}_{self.timestamp_str}"
        self.experiment_dir = Path(cfg.base_save_dir) / self.experiment_name
        self.experiment_dir.mkdir(parents=True, exist_ok=True)

        # Log files in the root experiment folder
        self.logger = setup_system_logger(self.experiment_dir, self.experiment_name)
        self.z80_traffic_file = self.experiment_dir / f"{self.experiment_name}_z80_traffic.txt"
        
        cfg_snapshot = dataclasses.asdict(cfg)
        cfg_snapshot["serial_number"] = self.serial_number
        cfg_snapshot["run_timestamp"] = self.timestamp_str

        self.telemetry = ExperimentTelemetryLogger(
            output_dir=self.experiment_dir,
            experiment_name=self.experiment_name,
            config_snapshot=cfg_snapshot
        )
        self.storage = DataStorage(cfg.base_save_dir, self.experiment_name)

        # Subsystems
        self.vna = None
        self.psu = None
        self.dut = None
        self.temp_sensor = None
        self.ml_classifier = None
        self.plotter = None
        self.z80 = None

    def initialize_hardware(self):
        """Initializes hardware non-blockingly, allowing optional devices to be skipped."""
        self.logger.info(f"Initializing Test Suite for DUT: {self.serial_number}")
        self.logger.info(f"Output Directory: {self.experiment_dir}")

        # 1. NanoVNA (Required)
        self.logger.info(f"Connecting to NanoVNA on {self.cfg.vna_port}...")
        self.vna = VNADevice(self.cfg.start_freq, self.cfg.stop_freq, self.cfg.points, self.cfg.vna_port)
        self.vna.connect()

        # 2. Power Supply (Optional)
        if self.cfg.enable_power_supply:
            try:
                self.logger.info(f"Connecting to PSU on {self.cfg.power_supply_port}...")
                self.psu = GPD2303S(self.cfg.power_supply_port)
                self.psu.connect()
                self.dut = DUTPowerController(self.psu, channel=self.cfg.dut_channel)
                self.dut.power_off()
            except Exception as e:
                self.logger.warning(f"Power Supply not available ({e}). Proceeding in manual power mode.")
                self.psu = None
                self.dut = None
        else:
            self.logger.info("Automated power supply disabled in config.")

        # 3. Ambient Sensor (Optional)
        if self.cfg.enable_ambient_temp:
            try:
                self.logger.info(f"Connecting to ambient sensor on {self.cfg.ambient_temp_port}...")
                self.temp_sensor = TemperatureSensor(self.cfg.ambient_temp_port)
                self.temp_sensor.connect()
            except Exception as e:
                self.logger.warning(f"Ambient temperature sensor not detected ({e}). Skipping sensor.")
                self.temp_sensor = None

        # 4. ML Aging Classifier
        if self.cfg.enable_ml_evaluation:
            try:
                self.logger.info(f"Loading {self.cfg.ml_model_type.upper()} model from {self.cfg.ml_model_dir}...")
                self.ml_classifier = RFAgingClassifier(self.cfg.ml_model_dir, self.cfg.ml_model_type)
                self.ml_classifier.load_model()
            except Exception as e:
                self.logger.warning(f"ML Classifier could not be loaded ({e}). Evaluation disabled.")
                self.ml_classifier = None

        # 5. Live Plotter GUI
        if self.cfg.enable_live_plot and not self.plotter:
            try:
                self.logger.info("Initializing Live Plotter GUI...")
                self.plotter = LivePlotter(self.cfg.start_freq, self.cfg.stop_freq)
            except Exception as e:
                self.logger.warning(f"Could not initialize GUI plotter ({e}). Continuing in headless mode.")
                self.plotter = None

        # 6. Z80 Target Controller (Optional)
        try:
            self.z80 = Z80Controller(
                port=self.cfg.z80_serial_port,
                baud_rate=self.cfg.z80_baud_rate,
                binary_path=self.cfg.z80_binary_path,
                binary_addr=self.cfg.z80_binary_addr,
                kickoff_byte=self.cfg.z80_kickoff_byte
            )
            self.z80.connect()
        except Exception as e:
            self.logger.warning(f"Z80 serial port not open ({e}). Serial automation will be disabled.")
            self.z80 = None

    def capture_single_sweep(self, stage_name: str) -> dict:
        t_dt = datetime.now()
        t_epoch = time.time()

        ambient_val = self.temp_sensor.read_temperature() if self.temp_sensor else None
        dut_stats = self.dut.read_telemetry() if self.dut else {}

        # Capture RF
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

        # Write to stage subfolder
        s2p_path = self.storage.save_record(record, subfolder=stage_name)
        file_hash = ExperimentTelemetryLogger.compute_sha256(s2p_path)

        # ML Inference
        ml_res = {
            'class': None,
            'label': 'Disabled',
            'confidence': None,
            'probabilities': [None, None, None]
        }
        if self.ml_classifier:
            ml_res = self.ml_classifier.evaluate(record)

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
            # PROMPT 1 -> 2: MANUAL POWER CONNECTION
            # ----------------------------------------------------
            wait_for_user_with_flash(f"[{self.serial_number}] Step 1 Complete -> MANUALLY CONNECT POWER FOR STEP 2")

            # ----------------------------------------------------
            # STAGE 2: POWERED (IDLE) - MANUAL
            # ----------------------------------------------------
            self.logger.info(f"[{self.serial_number}] Commencing Step 2: Powered Idle (Manual Power Active)...")
            self.run_stage_for_duration("2_POWERED_IDLE", self.step_duration_s)

            # ----------------------------------------------------
            # AUTO ADVANCE 2 -> 3: TERMINAL BANNER & SYNC CHECK
            # ----------------------------------------------------
            print("\n" + "=" * 70)
            print(">>> NOTIFICATION: STEP 3 (POWERED + Z80 ACTIVE) STARTING <<<")
            print("=" * 70 + "\n")
            self.logger.info(">>> STEP 3 START: Programming Z80 & Validating Serial Traffic <<<")

            if self.z80:
                # 1. Program and start RUN
                self.logger.info("Initiating Z80 BLOAD transfer...")
                if not self.z80.program_and_run():
                    self.logger.error("Z80 BLOAD programming failed!")

                # 2. Release wait state with kickoff byte
                self.z80.send_kickoff()

                # 3. Interactive Sync Verification (0x01, 0x01, 0x01, 0x01)
                while True:
                    print("\nChecking for Z80 sync signal (expecting 01 01 01 01)...")
                    if self.z80.verify_heartbeat(count=4, timeout_s=6.0):
                        print("\033[92;1m[Z80 SYNC VERIFIED: Received 01 01 01 01]\033[0m")
                        self.logger.info("Z80 sync validated successfully.")
                        break
                    
                    print("\033[91;1m[WARNING] Sync bytes (01 01 01 01) not observed from Z80!\033[0m")
                    choice = input("Select action: [R]etry handshake, [P]roceed anyway, or [A]bort test? ").strip().lower()
                    if choice == 'a':
                        raise KeyboardInterrupt("Test run aborted by user due to missing Z80 sync.")
                    elif choice == 'p':
                        self.logger.warning("Proceeding with Step 3 without validated Z80 sync.")
                        break
                    # If 'r', loop runs verify_heartbeat again

                # 4. Begin continuous logging to <experiment_name>_z80_traffic.txt
                self.logger.info(f"Logging Z80 traffic to: {self.z80_traffic_file.name}")
                self.z80.start_monitoring(self.z80_traffic_file)
            else:
                self.logger.warning("No Z80 controller active. Skipping Z80 programming and traffic capture.")

            # ----------------------------------------------------
            # STAGE 3: POWERED + Z80 ACTIVE
            # ----------------------------------------------------
            self.run_stage_for_duration("3_POWERED_Z80_ACTIVE", self.step_duration_s)

            # ----------------------------------------------------
            # PROMPT 3 -> 4
            # ----------------------------------------------------
            wait_for_user_with_flash(f"[{self.serial_number}] Step 3 Complete -> ENABLE COOLING FOR STEP 4")

            # ----------------------------------------------------
            # STAGE 4: POWERED + Z80 ACTIVE + COOLING
            # ----------------------------------------------------
            # Z80 monitoring remains actively writing to the same root traffic file
            self.run_stage_for_duration("4_POWERED_Z80_ACTIVE_COOLING", self.step_duration_s)

            self.logger.info(f">>> All 4 stages finished successfully for DUT {self.serial_number}. <<<")

        except KeyboardInterrupt:
            self.logger.warning("Test run halted by operator.")
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
            self.logger.info(f"Test complete. Output saved in {self.experiment_dir}")


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