# config.py
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

@dataclass
class AppConfig:
    # Feature Toggles
    enable_ambient_temp: bool = False
    enable_power_supply: bool = True
    enable_rtd_heater: bool = True
    enable_live_plot: bool = True
    enable_anomaly_detection: bool = False
    enable_blockchain_sync: bool = False

    # Experiment Loop Parameters
    loop_interval_seconds: float = 1.0
    base_save_dir: str = "/home/chiptamper/chiptamper/data"
    experiment_name: str = "Z80_prelim_mod3_sn30"

    # VNA Parameters
    start_freq: float = 500e3
    stop_freq: float = 4e9
    points: int = 201

    # Hardware Serial Ports
    ambient_temp_port: str = "/dev/ttyUSB1"
    power_supply_port: str = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AI03DT65-if00-port0"

    # DUT Power Configuration (CH2)
    dut_channel: int = 2
    dut_voltage: float = 3.3
    dut_current_limit: float = 0.500

    # RTD Heater Configuration (CH1)
    rtd_steps: List[float] = field(default_factory=lambda: [65.0, 80.0, 95.0, 110.0, 120.0])
    rtd_dwell_seconds: float = 600.0
    rtd_tolerance: float = 1.0
    rtd_stable_samples: int = 3
    rtd_kp: float = 0.0020
    rtd_ki: float = 0.0010
    rtd_kd: float = 0.0000