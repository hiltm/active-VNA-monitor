# config.py
from dataclasses import dataclass, field
from typing import List

@dataclass
class AppConfig:
    # Feature Toggles
    enable_ambient_temp: bool = False
    enable_power_supply: bool = True
    enable_rtd_heater: bool = True
    enable_live_plot: bool = True
    enable_anomaly_detection: bool = False
    enable_ml_evaluation: bool = True
    enable_blockchain_sync: bool = False

    # ML Inference Settings
    ml_model_dir: str = "/home/chiptamper/chiptamper/git/active-VNA-monitor/models"
    ml_model_type: str = "rf"  # Options: 'rf', 'knn', 'svm'

    # Blockchain Provenance Ledger (used when enable_blockchain_sync = True)
    blockchain_rpc_url: str = "http://127.0.0.1:8545"
    blockchain_contract_address_file: str = (
        "/home/chiptamper/chiptamper/git/active-VNA-monitor/"
        "blockchain/artifacts_on_chain/contract-address.json"
    )
    blockchain_chain_id: int | None = None
    blockchain_private_key: str | None = None

    # Experiment Loop Parameters
    loop_interval_seconds: float = 1.0
    base_save_dir: str = "/home/chiptamper/chiptamper/data"
    experiment_name: str = "Z80_prelim_mod3_sn30"

    # VNA Parameters
    start_freq: float = 500e3
    stop_freq: float = 4e9
    points: int = 201

    # Hardware Serial Ports
    vna_port: str = "/dev/serial/by-id/usb-SYSJOINT_NanoVnaPro_Virtual_ComPort_00000000001A-if00"
    power_supply_port: str = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AI03DT65-if00-port0"
    ambient_temp_port: str = "/dev/ttyUSB1"
    z80_serial_port: str = "/dev/ttyUSB2"  # Set to your Z80 UART USB adapter port

    # DUT Power Configuration (CH2)
    dut_channel: int = 2
    dut_voltage: float = 5.0
    dut_current_limit: float = 0.500

    # Z80 Target Configuration
    z80_baud_rate: int = 9600
    z80_binary_path: str = (
        "/home/chiptamper/chiptamper/git/active-VNA-monitor/"
        "targets/Z80/Test1Sec_ND.bin"
    )
    z80_binary_addr: str = "0800"
    z80_kickoff_byte: int = 5

    # RTD Heater Configuration (CH1)
    rtd_steps: List[float] = field(default_factory=lambda: [65.0, 80.0, 95.0, 110.0, 120.0])
    rtd_dwell_seconds: float = 600.0
    rtd_tolerance: float = 1.0
    rtd_stable_samples: int = 3
    rtd_kp: float = 0.0020
    rtd_ki: float = 0.0010
    rtd_kd: float = 0.0000