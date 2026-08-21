# main.py
import sys
import time
import dataclasses
from pathlib import Path
from datetime import datetime

from config import AppConfig
from core.models import MeasurementRecord
from core.storage import DataStorage
from core.logger import setup_system_logger, ExperimentTelemetryLogger
from drivers.vna_driver import VNADevice
from drivers.temp_sensor import TemperatureSensor
from drivers.gpd2303s import GPD2303S
from drivers.dut_power import DUTPowerController
from drivers.rtd_heater import RTDHeaterController
from ui.visualizer import LivePlotter


class ExperimentController:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.experiment_dir = Path(cfg.base_save_dir) / cfg.experiment_name
        self.logger = setup_system_logger(self.experiment_dir, cfg.experiment_name)
        
        self.telemetry = ExperimentTelemetryLogger(
            output_dir=self.experiment_dir,
            experiment_name=cfg.experiment_name,
            config_snapshot=dataclasses.asdict(cfg)
        )
        self.storage = DataStorage(cfg.base_save_dir, cfg.experiment_name)

        self.vna = None
        self.ambient_sensor = None
        self.psu = None
        self.dut = None
        self.heater = None
        self.plotter = None

    def connect_all_hardware(self, is_recovery: bool = False) -> bool:
        """Attempts to initialize and configure all active instruments."""
        try:
            # 1. NanoVNA
            if not self.vna:
                self.logger.info("Connecting to VNA...")
                self.vna = VNADevice(self.cfg.start_freq, self.cfg.stop_freq, self.cfg.points)
                self.vna.connect()

            # 2. Ambient Sensor
            if self.cfg.enable_ambient_temp and not self.ambient_sensor:
                self.logger.info(f"Connecting to ambient sensor on {self.cfg.ambient_temp_port}...")
                self.ambient_sensor = TemperatureSensor(port=self.cfg.ambient_temp_port)
                self.ambient_sensor.connect()

            # 3. Power Supply & Sub-Controllers
            if (self.cfg.enable_power_supply or self.cfg.enable_rtd_heater) and not self.psu:
                self.logger.info(f"Connecting to PSU on {self.cfg.power_supply_port}...")
                self.psu = GPD2303S(port=self.cfg.power_supply_port)
                self.psu.connect()

                if self.cfg.enable_power_supply:
                    self.dut = DUTPowerController(self.psu, channel=self.cfg.dut_channel)
                    self.dut.power_on(voltage=self.cfg.dut_voltage, current_limit=self.cfg.dut_current_limit)
                    self.logger.info(f"DUT power restored on CH{self.cfg.dut_channel}.")

                if self.cfg.enable_rtd_heater:
                    # Preserve existing PID/schedule state if recovering
                    if not self.heater:
                        self.heater = RTDHeaterController(
                            psu=self.psu,
                            steps=self.cfg.rtd_steps,
                            dwell_seconds=self.cfg.rtd_dwell_seconds,
                            tolerance=self.cfg.rtd_tolerance,
                            stable_samples=self.cfg.rtd_stable_samples,
                            kp=self.cfg.rtd_kp, ki=self.cfg.rtd_ki, kd=self.cfg.rtd_kd
                        )
                    else:
                        self.heater.psu = self.psu
                    
                    self.heater.initialize_hardware()
                    self.logger.info(f"RTD Heater re-initialized on CH{self.heater.CHANNEL}.")

            # 4. Plotter
            if self.cfg.enable_live_plot and not self.plotter:
                self.plotter = LivePlotter(self.cfg.start_freq, self.cfg.stop_freq)

            if is_recovery:
                self.logger.info(">>> RECOVERY SUCCESSFUL: All hardware re-armed and online. <<<")
            return True

        except Exception as e:
            self.logger.warning(f"Connection attempt failed: {e}")
            self.disconnect_all(silent=True)
            return False

    def disconnect_all(self, silent: bool = False):
        """Safely closes connections to allow clean re-opening."""
        if not silent:
            self.logger.info("Releasing hardware handles...")
        try:
            if self.dut: self.dut.power_off()
        except Exception: pass
        try:
            if self.psu: self.psu.close()
        except Exception: pass
        try:
            if self.ambient_sensor: self.ambient_sensor.close()
        except Exception: pass
        try:
            if self.vna: self.vna.close()
        except Exception: pass

        self.psu = None
        self.dut = None
        self.ambient_sensor = None
        self.vna = None

    def wait_for_hardware_recovery(self, poll_interval: float = 3.0):
        """Blocks and polls until dropped instruments are back online."""
        self.logger.error(">>> POWER/COMMUNICATION LOSS DETECTED. Entering recovery loop... <<<")
        self.disconnect_all(silent=True)

        # Log power event marker to CSV
        self.telemetry.log_sweep({
            "iso_time": datetime.now().isoformat(),
            "epoch_time": time.time(),
            "touchstone_file": "POWER_LOSS_EVENT",
            "touchstone_sha256": "N/A",
            "ambient_temp_c": None,
            "rtd_setpoint_c": None,
            "rtd_measured_temp_c": None,
            "rtd_resistance_ohm": None,
            "rtd_current_a": None,
            "rtd_voltage_v": None,
            "rtd_phase": "OFFLINE_POWER_CYCLE",
            "dut_voltage_v": None,
            "dut_current_ma": None,
            "dut_power_mw": None,
            "anomaly_score": 1.0,
            "anomaly_flag": True
        })

        retry_count = 1
        while True:
            self.logger.info(f"Recovery attempt #{retry_count}: checking instrument availability...")
            if self.connect_all_hardware(is_recovery=True):
                # Log recovery event to CSV
                self.telemetry.log_sweep({
                    "iso_time": datetime.now().isoformat(),
                    "epoch_time": time.time(),
                    "touchstone_file": "POWER_RECOVERY_SUCCESS",
                    "touchstone_sha256": "N/A",
                    "ambient_temp_c": None,
                    "rtd_setpoint_c": None,
                    "rtd_measured_temp_c": None,
                    "rtd_resistance_ohm": None,
                    "rtd_current_a": None,
                    "rtd_voltage_v": None,
                    "rtd_phase": "ONLINE_RESUMED",
                    "dut_voltage_v": None,
                    "dut_current_ma": None,
                    "dut_power_mw": None,
                    "anomaly_score": 0.0,
                    "anomaly_flag": False
                })
                break
            retry_count += 1
            time.sleep(poll_interval)

    def run(self):
        self.logger.info(f"Initializing experiment suite: {self.cfg.experiment_name}")
        
        # Initial boot connection loop
        while not self.connect_all_hardware():
            self.logger.warning("Hardware not ready on boot. Retrying in 5 seconds...")
            time.sleep(5.0)

        start_time = time.monotonic()
        self.logger.info("Starting acquisition loop. Press Ctrl+C to abort.")

        try:
            while True:
                t_epoch = time.time()
                t_now_s = time.monotonic() - start_time
                t_dt = datetime.now()

                try:
                    # 1. Update Ambient Temperature
                    ambient_val = self.ambient_sensor.read_temperature() if self.ambient_sensor else None

                    # 2. Update Heater State Machine
                    heater_stats = {}
                    if self.heater:
                        heater_stats = self.heater.update_tick(t_now_s)

                    # 3. Read DUT Telemetry
                    dut_stats = {}
                    if self.dut:
                        dut_stats = self.dut.read_telemetry()

                    # 4. Execute VNA RF Sweep
                    freqs, s11, s21, s12, s22 = self.vna.sweep()

                except Exception as hw_err:
                    self.logger.error(f"Instrument communication error during sweep: {hw_err}")
                    self.wait_for_hardware_recovery()
                    continue

                # 5. Encapsulate Data Record
                record = MeasurementRecord(
                    timestamp=t_dt,
                    freqs=freqs,
                    s11=s11,
                    s21=s21,
                    s12=s12,
                    s22=s22,
                    temperature=heater_stats.get("temp", ambient_val),
                    power_state={**dut_stats, "rtd_stats": heater_stats}
                )

                # 6. Save Data Artifact (.s2p) & Checksum
                s2p_path = self.storage.save_record(record)
                file_hash = ExperimentTelemetryLogger.compute_sha256(s2p_path)

                # 7. Log Synchronized Telemetry Row
                self.telemetry.log_sweep({
                    "iso_time": t_dt.isoformat(),
                    "epoch_time": t_epoch,
                    "touchstone_file": s2p_path.name,
                    "touchstone_sha256": file_hash,
                    "ambient_temp_c": ambient_val,
                    "rtd_setpoint_c": heater_stats.get("setpoint"),
                    "rtd_measured_temp_c": heater_stats.get("temp"),
                    "rtd_resistance_ohm": heater_stats.get("resistance"),
                    "rtd_current_a": heater_stats.get("amps"),
                    "rtd_voltage_v": heater_stats.get("volts"),
                    "rtd_phase": heater_stats.get("phase"),
                    "dut_voltage_v": dut_stats.get("dut_voltage_v"),
                    "dut_current_ma": (dut_stats.get("dut_current_a") or 0.0) * 1000,
                    "dut_power_mw": (dut_stats.get("dut_power_w") or 0.0) * 1000,
                    "anomaly_score": 0.0,
                    "anomaly_flag": False
                })

                # 8. Update UI
                if self.plotter:
                    self.plotter.update(record)

                time.sleep(self.cfg.loop_interval_seconds)

        except KeyboardInterrupt:
            self.logger.info("Experiment stopped by user.")
        finally:
            self.disconnect_all()
            if self.plotter: self.plotter.close()
            self.logger.info("Suite shutdown complete.")


if __name__ == "__main__":
    config = AppConfig()
    runner = ExperimentController(config)
    runner.run()