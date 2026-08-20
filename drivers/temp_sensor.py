# drivers/temp_sensor.py
import serial
import time
import logging

logger = logging.getLogger(__name__)

class TemperatureSensor:
    def __init__(self, port: str = '/dev/ttyUSB1', baudrate: int = 9600, min_valid_temp: float = 20.0):
        self.port = port
        self.baudrate = baudrate
        self.min_valid_temp = min_valid_temp
        self.ser = None

    def connect(self):
        try:
            self.ser = serial.Serial(self.port, self.baudrate, timeout=1)
            logger.info(f"Connected to temperature sensor on {self.port}")
        except Exception as e:
            logger.warning(f"Failed to open temperature sensor port {self.port}: {e}")
            self.ser = None

    def read_temperature(self, max_retries: int = 3) -> float | None:
        if not self.ser:
            return None

        for _ in range(max_retries):
            try:
                self.ser.reset_input_buffer()
                line = self.ser.readline().decode('utf-8', errors='ignore').strip()
                numeric_part = "".join(ch for ch in line if ch.isdigit() or ch in ".-")
                if numeric_part:
                    val = float(numeric_part)
                    if val >= self.min_valid_temp:
                        return val
                time.sleep(0.2)
            except Exception as e:
                logger.error(f"Error reading temperature: {e}")
                break
        return None

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
