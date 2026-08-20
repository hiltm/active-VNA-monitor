# drivers/gpd2303s.py
import serial
import time
import logging

logger = logging.getLogger(__name__)

class GPD2303S:
    """
    Physical serial driver for GW Instek GPD-2303S dual-channel PSU.
    Default: 9600 8N1, newline-terminated ASCII.
    """
    BAUD = 9600
    TIMEOUT = 2.0
    MAX_VOLTAGE = 30.0   # V (hardware max)
    MAX_CURRENT = 3.0    # A (hardware max)

    def __init__(self, port: str = "/dev/ttyUSB0"):
        self.port = port
        self._ser = None

    def connect(self):
        if self._ser is None or not self._ser.is_open:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.BAUD,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.TIMEOUT,
            )
            time.sleep(0.2)
            self._ser.reset_input_buffer()
            logger.info(f"Connected to GPD-2303S on {self.port}")

    def _write(self, cmd: str):
        if not self._ser:
            raise ConnectionError("Serial port not connected.")
        self._ser.write((cmd + "\n").encode("ascii"))
        time.sleep(0.05)

    def _query(self, cmd: str) -> str:
        self._write(cmd)
        raw = self._ser.readline()
        return raw.decode("ascii", errors="replace").strip()

    @staticmethod
    def _parse_float(resp: str) -> float:
        """Strip trailing unit chars before converting to float."""
        try:
            return float(resp.strip().rstrip("VAOhms "))
        except ValueError:
            logger.warning("Bad PSU response format: %r", resp)
            return 0.0

    def set_voltage(self, channel: int, volts: float):
        volts = max(0.0, min(volts, self.MAX_VOLTAGE))
        self._write(f"VSET{channel}:{volts:.3f}")

    def set_current(self, channel: int, amps: float):
        amps = max(0.0, min(amps, self.MAX_CURRENT))
        self._write(f"ISET{channel}:{amps:.4f}")

    def measure_voltage(self, channel: int) -> float:
        return self._parse_float(self._query(f"VOUT{channel}?"))

    def measure_current(self, channel: int) -> float:
        return self._parse_float(self._query(f"IOUT{channel}?"))

    def output_on(self):
        self._write("OUT1")

    def output_off(self):
        self._write("OUT0")

    def close(self):
        if self._ser and self._ser.is_open:
            try:
                self.output_off()
            finally:
                self._ser.close()
                logger.info("Closed GPD-2303S serial connection.")
