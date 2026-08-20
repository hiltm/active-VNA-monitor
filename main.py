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


def run_experiment(cfg: AppConfig):
    # 1. Setup Logging & Run Manifest
    experiment_dir = Path(cfg.base_save_dir) / cfg.experiment_name
    logger = setup_system_logger(experiment_dir, cfg.experiment_name)
    logger.info(f"Initializing experiment suite: {cfg.experiment_name}")

    telemetry = ExperimentTelemetryLogger(
        output_dir=experiment_dir,
        experiment_name=cfg.experiment_name,
        config_snapshot=dataclasses.asdict(cfg)
    )

    # 2. Initialize Subsystems
    storage = DataStorage(cfg.base_save_dir, cfg.experiment_name)
    
    # VNA Setup
    vna = VNADevice(cfg.start_freq, cfg.stop_freq, cfg.points)
    try:
        vna.connect()
        logger.info("Connected to VNA.")
    except Exception as e:
        logger.error(f"Failed to connect to VNA: {e}")
        return

    # Ambient Sensor Setup
    ambient_sensor = None
    if cfg.enable_ambient_temp:
        ambient_sensor = TemperatureSensor(port=cfg.ambient_temp_port)
        ambient_sensor.connect()

    # Power Supply Subsystem Setup
    psu = None
    dut = None
    heater = None
    if cfg.enable_power_supply or cfg.enable_rtd_heater:
        try:
            psu = GPD2303S(port=cfg.power_supply_port)
            psu.connect()

            if cfg.enable_power_supply:
                dut = DUTPowerController(psu, channel=cfg.dut_channel)
                dut.power_on(voltage=cfg.dut_voltage, current_limit=cfg.dut_current_limit)
                logger.info(f"DUT power active on CH{cfg.dut_channel} ({cfg.dut_voltage}V, {cfg.dut_current_limit}A).")

            if cfg.enable_rtd_heater:
                heater = RTDHeaterController(
                    psu=psu,
                    steps=cfg.rtd_steps,
                    dwell_seconds=cfg.rtd_dwell_seconds,
                    tolerance=cfg.rtd_tolerance,
                    stable_samples=cfg.rtd_stable_samples,
                    kp=cfg.rtd_kp, ki=cfg.rtd_ki, kd=cfg.rtd_kd
                )
                heater.initialize_hardware()
                logger.info(f"RTD stepped heater initialized on CH{heater.CHANNEL}.")
        except Exception as e:
            logger.error(f"Failed initializing power supply subsystem: {e}")

    # Visualizer Setup
    plotter = None
    if cfg.enable_live_plot:
        plotter = LivePlotter(cfg.start_freq, cfg.stop_freq)

    # 3. Main Acquisition Loop
    start_time = time.monotonic()
    logger.info("Starting acquisition loop. Press Ctrl+C to abort.")

    try:
        while True:
            t_epoch = time.time()
            t_now_s = time.monotonic() - start_time
            t_dt = datetime.now()

            # A. Update Ambient Temperature
            ambient_val = ambient_sensor.read_temperature() if ambient_sensor else None

            # B. Update Heater State Machine
            heater_stats = {}
            if heater:
                heater_stats = heater.update_tick(t_now_s)

            # C. Read DUT Telemetry
            dut_stats = {}
            if dut:
                dut_stats = dut.read_telemetry()

            # D. Execute VNA RF Sweep
            try:
                freqs, s11, s21, s12, s22 = vna.sweep()
            except Exception as e:
                logger.warning(f"VNA sweep failed at t={t_now_s:.1f}s: {e}")
                time.sleep(cfg.loop_interval_seconds)
                continue

            # E. Encapsulate Data Record
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

            # F. Save Data Artifact (.s2p) & Compute Checksum
            s2p_path = storage.save_record(record)
            file_hash = ExperimentTelemetryLogger.compute_sha256(s2p_path)

            # G. Log Synchronized Telemetry Row
            telemetry.log_sweep({
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

            # H. Update Display
            if plotter:
                plotter.update(record)

            # Rate Limiting
            time.sleep(cfg.loop_interval_seconds)

    except KeyboardInterrupt:
        logger.info("Experiment interrupted by user.")
    finally:
        logger.info("Commencing safe hardware shutdown...")
        if dut:
            dut.power_off()
        if psu:
            psu.close()
        if ambient_sensor:
            ambient_sensor.close()
        if vna:
            vna.close()
        if plotter:
            plotter.close()
        logger.info("Suite shutdown complete.")


if __name__ == "__main__":
    config = AppConfig()
    run_experiment(config)