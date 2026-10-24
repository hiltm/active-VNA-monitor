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
from plugins.ml_engine import RFAgingClassifier
from plugins.blockchain_ledger import BlockchainLedger


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

        # Hardware & Plugin Handles
        self.vna = None
        self.ambient_sensor = None
        self.psu = None
        self.dut = None
        self.heater = None
        self.plotter = None
        self.ml_classifier = None
        self.ledger = None

    def connect_all_hardware(self, is_recovery: bool = False) -> bool:
        """Initializes and connects all active experimental subsystems."""
        try:
            # 1. NanoVNA Connection
            if not self.vna:
                self.logger.info(f"Connecting to NanoVNA (Port: {self.cfg.vna_port})...")
                self.vna = VNADevice(
                    start_freq=self.cfg.start_freq,
                    stop_freq=self.cfg.stop_freq,
                    points=self.cfg.points,
                    port=self.cfg.vna_port
                )
                self.vna.connect()

            # 2. Ambient Sensor Connection
            if self.cfg.enable_ambient_temp and not self.ambient_sensor:
                self.logger.info(f"Connecting to ambient sensor on {self.cfg.ambient_temp_port}...")
                self.ambient_sensor = TemperatureSensor(port=self.cfg.ambient_temp_port)
                self.ambient_sensor.connect()

            # 3. Dual-Channel Power Supply & Sub-Controllers
            if (self.cfg.enable_power_supply or self.cfg.enable_rtd_heater) and not self.psu:
                self.logger.info(f"Connecting to PSU on {self.cfg.power_supply_port}...")
                self.psu = GPD2303S(port=self.cfg.power_supply_port)
                self.psu.connect()

                # DUT Channel (CH2)
                if self.cfg.enable_power_supply:
                    self.dut = DUTPowerController(self.psu, channel=self.cfg.dut_channel)
                    self.dut.power_on(voltage=self.cfg.dut_voltage, current_limit=self.cfg.dut_current_limit)
                    self.logger.info(
                        f"DUT power active on CH{self.cfg.dut_channel} "
                        f"({self.cfg.dut_voltage}V, {self.cfg.dut_current_limit}A)."
                    )

                # RTD Heating Element (CH1)
                if self.cfg.enable_rtd_heater:
                    if not self.heater:
                        self.heater = RTDHeaterController(
                            psu=self.psu,
                            steps=self.cfg.rtd_steps,
                            dwell_seconds=self.cfg.rtd_dwell_seconds,
                            tolerance=self.cfg.rtd_tolerance,
                            stable_samples=self.cfg.rtd_stable_samples,
                            kp=self.cfg.rtd_kp,
                            ki=self.cfg.rtd_ki,
                            kd=self.cfg.rtd_kd,
                            measure_samples=self.cfg.rtd_measure_samples,
                            sample_spacing_s=self.cfg.rtd_sample_spacing_s,
                            measure_settle_seconds=self.cfg.rtd_measure_settle_seconds,
                        )
                    else:
                        self.heater.psu = self.psu
                    
                    self.heater.initialize_hardware()
                    self.logger.info(f"RTD Heater initialized on CH{self.heater.CHANNEL}.")

            # 4. Live GUI Visualizer
            if self.cfg.enable_live_plot and not self.plotter:
                self.plotter = LivePlotter(self.cfg.start_freq, self.cfg.stop_freq)

            # 5. RF Reflectometry Aging ML Classifier
            if self.cfg.enable_ml_evaluation and not self.ml_classifier:
                self.logger.info(f"Loading {self.cfg.ml_model_type.upper()} classifier from {self.cfg.ml_model_dir}...")
                self.ml_classifier = RFAgingClassifier(
                    model_dir=self.cfg.ml_model_dir,
                    model_type=self.cfg.ml_model_type
                )
                self.ml_classifier.load_model()

            # 6. Blockchain Provenance Ledger
            if self.cfg.enable_blockchain_sync and not self.ledger:
                self.logger.info(
                    f"Connecting to blockchain ledger at {self.cfg.blockchain_rpc_url}..."
                )
                self.ledger = BlockchainLedger(
                    rpc_url=self.cfg.blockchain_rpc_url,
                    contract_address_file=self.cfg.blockchain_contract_address_file,
                    chain_id=self.cfg.blockchain_chain_id,
                    private_key=self.cfg.blockchain_private_key,
                )
                self.ledger.connect()

            if is_recovery:
                self.logger.info(">>> RECOVERY SUCCESSFUL: All instruments restored and online. <<<")
            return True

        except Exception as e:
            self.logger.warning(f"Subsystem initialization failed: {e}")
            self.disconnect_all(silent=True)
            return False

    def disconnect_all(self, silent: bool = False):
        """Safely shuts off power outputs and releases OS serial communication ports."""
        if not silent:
            self.logger.info("Closing all active hardware handles...")
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

        try:
            if self.ledger: self.ledger.close()
        except Exception: pass

        self.psu = None
        self.dut = None
        self.ambient_sensor = None
        self.vna = None
        self.ledger = None

    def wait_for_hardware_recovery(self, poll_interval: float = 3.0):
        """Blocks execution and polls until power-cycled hardware becomes reachable again."""
        self.logger.error(">>> POWER/COMMUNICATION INTERRUPTION DETECTED. Entering recovery loop... <<<")
        self.disconnect_all(silent=True)

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
            "ml_class": None,
            "ml_label": "OFFLINE",
            "ml_confidence": None,
            "ml_prob_fresh": None,
            "ml_prob_light_aged": None,
            "ml_prob_heavy_aged": None,
            "blockchain_tx": None
        })

        retry_count = 1
        while True:
            self.logger.info(f"Recovery attempt #{retry_count}: verifying instrument availability...")
            if self.connect_all_hardware(is_recovery=True):
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
                    "ml_class": None,
                    "ml_label": "RESUMED",
                    "ml_confidence": None,
                    "ml_prob_fresh": None,
                    "ml_prob_light_aged": None,
                    "ml_prob_heavy_aged": None,
                    "blockchain_tx": None
                })
                break
            retry_count += 1
            time.sleep(poll_interval)

    def run(self):
        self.logger.info(f"Starting experiment suite: {self.cfg.experiment_name}")
        
        while not self.connect_all_hardware():
            self.logger.warning("Hardware not ready on boot. Retrying in 5 seconds...")
            time.sleep(5.0)

        start_time = time.monotonic()
        self.logger.info("Entering active acquisition loop. Press Ctrl+C to stop.")

        try:
            while True:
                t_epoch = time.time()
                t_now_s = time.monotonic() - start_time
                t_dt = datetime.now()

                try:
                    # 1. Read Ambient Sensor
                    ambient_val = self.ambient_sensor.read_temperature() if self.ambient_sensor else None

                    # 2. Update Thermal Stepped PID Loop
                    heater_stats = {}
                    if self.heater:
                        heater_stats = self.heater.update_tick(t_now_s)

                    # 3. Read DUT Power Consumption
                    dut_stats = {}
                    if self.dut:
                        dut_stats = self.dut.read_telemetry()

                    # 4. Perform VNA RF Sweep
                    freqs, s11, s21, s12, s22 = self.vna.sweep()

                except Exception as hw_err:
                    self.logger.error(f"Hardware communication failure: {hw_err}")
                    self.wait_for_hardware_recovery()
                    continue

                # 5. Pack Measurement Record
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

                # 6. Save Touchstone File (.s2p) & Checksum
                s2p_path = self.storage.save_record(record)
                file_hash = ExperimentTelemetryLogger.compute_sha256(s2p_path)

                # 7. Real-Time ML Aging Classification
                ml_res = {
                    'class': None,
                    'label': 'Disabled',
                    'confidence': None,
                    'probabilities': [None, None, None]
                }
                if self.ml_classifier:
                    ml_res = self.ml_classifier.evaluate(record)
                    self.logger.info(
                        f"Inference: Class {ml_res['class']} ({ml_res['label']}) | "
                        f"Conf: {ml_res['confidence']:.1%} | "
                        f"P(C0)={ml_res['probabilities'][0]:.2f} "
                        f"P(C1)={ml_res['probabilities'][1]:.2f} "
                        f"P(C2)={ml_res['probabilities'][2]:.2f}"
                    )

                # 8. Append Synchronized Telemetry Row
                blockchain_tx = None
                if self.ledger:
                    blockchain_tx = self.ledger.record_measurement(
                        s2p_path=s2p_path,
                        sha256_hex=file_hash,
                        metadata={
                            "experiment": self.cfg.experiment_name,
                            "iso_time": t_dt.isoformat(),
                            "file_type": "s2p",
                            "ml_class": ml_res['class'],
                            "ml_label": ml_res['label'],
                            "rtd_setpoint_c": heater_stats.get("setpoint"),
                            "rtd_measured_temp_c": heater_stats.get("temp"),
                        },
                    )

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
                    "ml_class": ml_res['class'],
                    "ml_label": ml_res['label'],
                    "ml_confidence": ml_res['confidence'],
                    "ml_prob_fresh": ml_res['probabilities'][0],
                    "ml_prob_light_aged": ml_res['probabilities'][1],
                    "ml_prob_heavy_aged": ml_res['probabilities'][2],
                    "blockchain_tx": blockchain_tx
                })

                # 9. Update Live Plotter GUI
                if self.plotter:
                    self.plotter.update(record)

                time.sleep(self.cfg.loop_interval_seconds)

        except KeyboardInterrupt:
            self.logger.info("Acquisition interrupted by user.")
        finally:
            self.disconnect_all()
            if self.plotter:
                self.plotter.close()
            self.logger.info("Suite shutdown complete.")


if __name__ == "__main__":
    config = AppConfig()
    runner = ExperimentController(config)
    runner.run()