# drivers/rtd_heater.py
import time
import datetime
import csv
import logging
from simple_pid import PID

logger = logging.getLogger(__name__)

PT100_TABLE = {
    -200: 18.49, -150: 39.71, -100: 60.25, -50: 80.31, 0: 100.00,
    10: 103.90, 20: 107.79, 30: 111.67, 40: 115.54, 50: 119.40,
    60: 123.24, 70: 127.07, 80: 130.89, 90: 134.70, 100: 138.50,
    110: 142.29, 120: 146.06, 130: 149.82, 140: 153.58, 150: 157.31,
    160: 161.04, 170: 164.76, 180: 168.46, 190: 172.16, 200: 175.84,
    210: 179.51, 220: 183.17, 230: 186.82, 239: 190.09,
}
_R_TO_T = sorted((r, t) for t, r in PT100_TABLE.items())

def resistance_to_temperature(ohms: float) -> float:
    """Linearly interpolate the PT100 table to convert Ohms -> degC."""
    if ohms <= _R_TO_T[0][0]:
        return float(_R_TO_T[0][1])
    if ohms >= _R_TO_T[-1][0]:
        return float(_R_TO_T[-1][1])
    for i in range(len(_R_TO_T) - 1):
        r0, t0 = _R_TO_T[i]
        r1, t1 = _R_TO_T[i + 1]
        if r0 <= ohms <= r1:
            frac = (ohms - r0) / (r1 - r0)
            return t0 + frac * (t1 - t0)
    return float(_R_TO_T[-1][1])


class RTDHeaterController:
    CHANNEL = 1
    DRIVE_VOLTAGE_LIMIT = 20.0    # V compliance ceiling
    MIN_CURRENT = 0.050           # A (50 mA floor for readback resolution)
    MAX_HEATER_CURRENT = 0.100    # A (100 mA safety cap)
    LOOP_PERIOD = 5.0             # s
    SAFETY_MAX_TEMP = 130.0       # degC

    def __init__(
        self,
        psu,
        steps=(65.0, 80.0, 95.0, 110.0, 120.0),
        dwell_seconds: float = 600.0,
        tolerance: float = 1.0,
        stable_samples: int = 3,
        kp: float = 0.0020, ki: float = 0.0010, kd: float = 0.0000,
        log_writer=None, log_file=None
    ):
        self.psu = psu
        self.steps = list(steps)
        self.dwell_seconds = float(dwell_seconds)
        self.tolerance = float(tolerance)
        self.stable_samples = int(stable_samples)
        self._last_set_current = self.MIN_CURRENT
        self._log_writer = log_writer
        self._log_file = log_file

        self.pid = PID(
            Kp=kp, Ki=ki, Kd=kd,
            setpoint=self.steps[0],
            sample_time=self.LOOP_PERIOD,
            output_limits=(self.MIN_CURRENT, self.MAX_HEATER_CURRENT),
        )
        self.step_idx = 0
        self.arrived = False
        self.stable_count = 0
        self.dwell_start = None

    def initialize_hardware(self):
        self.psu.set_voltage(self.CHANNEL, self.DRIVE_VOLTAGE_LIMIT)
        self.psu.set_current(self.CHANNEL, self.MIN_CURRENT)
        self.psu.output_on()

    def measure_temperature(self) -> tuple[float, float, float]:
        """Returns (temperature_degC, resistance_ohm, current_A)."""
        volts = self.psu.measure_voltage(self.CHANNEL)
        amps = self.psu.measure_current(self.CHANNEL)
        if amps < 0.0005:
            amps = self._last_set_current
        resistance = volts / amps if amps > 0 else 0.0
        temperature = resistance_to_temperature(resistance)
        return temperature, resistance, amps

    def update_tick(self, t_now: float) -> dict:
        """Non-blocking single cycle execution (ideal for main loop integration)."""
        temp, resistance, amps = self.measure_temperature()
        volts = self.psu.measure_voltage(self.CHANNEL)

        # Safety clamp
        if temp >= self.SAFETY_MAX_TEMP:
            logger.error(f"SAFETY CUTOFF: {temp:.2f}°C exceeds limit. Clamping current.")
            self.psu.set_current(self.CHANNEL, self.MIN_CURRENT)
            return {"temp": temp, "phase": "OVERTEMP"}

        setpoint = self.steps[self.step_idx]
        error = setpoint - temp
        new_current = self.pid(temp)
        self._last_set_current = new_current
        self.psu.set_current(self.CHANNEL, new_current)

        # Step schedule state machine
        if not self.arrived:
            if abs(error) <= self.tolerance:
                self.stable_count += 1
                phase = f"settle {self.stable_count}/{self.stable_samples}"
                if self.stable_count >= self.stable_samples:
                    self.arrived = True
                    self.dwell_start = t_now
                    logger.info(f"Step {self.step_idx+1} reached ({setpoint}°C). Dwelling {self.dwell_seconds}s.")
            else:
                self.stable_count = 0
                phase = f"ramp {self.step_idx+1}"
        else:
            remaining = self.dwell_seconds - (t_now - self.dwell_start)
            if remaining <= 0:
                if self.step_idx + 1 < len(self.steps):
                    self.step_idx += 1
                    self.arrived = False
                    self.stable_count = 0
                    self.dwell_start = None
                    self.pid.setpoint = self.steps[self.step_idx]
                    phase = f"ramp {self.step_idx+1}"
                else:
                    phase = "hold-final"
            else:
                phase = f"dwell {remaining:.0f}s"

        return {
            "time_s": t_now, "setpoint": setpoint, "temp": temp,
            "resistance": resistance, "amps": amps, "volts": volts,
            "error": error, "phase": phase
        }
