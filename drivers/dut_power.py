# drivers/dut_power.py
import logging

logger = logging.getLogger(__name__)

class DUTPowerController:
    """Controls the Device Under Test supply line (typically Channel 2)."""
    def __init__(self, psu_driver, channel: int = 2):
        self.psu = psu_driver
        self.channel = channel

    def configure(self, voltage: float, current_limit: float):
        logger.info(f"Configuring DUT Power (CH{self.channel}): {voltage}V, {current_limit}A limit")
        self.psu.set_voltage(self.channel, voltage)
        self.psu.set_current(self.channel, current_limit)

    def power_on(self, voltage: float = 3.3, current_limit: float = 0.5):
        self.configure(voltage, current_limit)
        self.psu.output_on()

    def power_off(self):
        # Setting voltage and current to 0 safely shuts off channel without killing global output
        self.psu.set_voltage(self.channel, 0.0)
        self.psu.set_current(self.channel, 0.0)
        logger.info(f"DUT Power (CH{self.channel}) disabled.")

    def read_telemetry(self) -> dict:
        v = self.psu.measure_voltage(self.channel)
        i = self.psu.measure_current(self.channel)
        return {"dut_voltage_v": v, "dut_current_a": i, "dut_power_w": v * i}
