# drivers/rtd_heater.py
import time
import logging

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
    """
    Drives a single PT100 element that is used BOTH as a heater and as a
    temperature sensor on PSU channel 1.

    Because one element cannot heat and be measured accurately at the same
    instant, the loop time-multiplexes two phases driven off a wall clock:

      * HEAT    - push a (ramped) heating current through the PT100. Power is
                  I^2 * R, so current is the natural control knob. The voltage
                  limit is held high as a compliance ceiling.
      * MEASURE - drop to a small, fixed excitation current (~1 mA) so that
                  self-heating is negligible, let the reading settle, then read
                  V/I to get the true resistance -> temperature.

    The heating current is ramped up incrementally toward the active setpoint
    (final step = 120 degC) and held/trimmed once inside the tolerance band,
    bounded by hard current/voltage ceilings the user can raise experimentally.
    """

    CHANNEL = 1

    # ---- Drive limits (raise MAX_HEATER_CURRENT experimentally for your element) ----
    DRIVE_VOLTAGE_LIMIT = 20.0    # V, compliance ceiling during the heat phase
    EXCITATION_CURRENT = 0.001    # A, 1 mA known current used only for measuring
    MIN_HEATER_CURRENT = 0.010    # A, lowest heating current the ramp starts from
    MAX_HEATER_CURRENT = 0.300    # A, hard safety cap on heating current
    CURRENT_STEP = 0.010          # A, max current increment per decision (when far off)
    MIN_CURRENT_STEP = 0.001      # A, smallest increment used near the setpoint

    # Proportional approach: the current step scales with error until the error
    # exceeds this band, where it uses the full CURRENT_STEP. Smaller band =
    # gentler, slower final approach with less overshoot.
    PROPORTIONAL_BAND = 30.0      # degC

    # Below this per-cycle temperature rise (degC) while still under setpoint,
    # the element is treated as stalled and the ramp keeps pushing to the cap.
    STALL_DELTA_T = 0.3           # degC per cycle
    # Above this per-cycle rise, hold current steady and let temperature catch
    # up rather than adding more current (overshoot protection).
    FAST_RISE_DELTA_T = 8.0       # degC per cycle

    # ---- Phase timing (wall clock, independent of update_tick call rate) ----
    HEAT_SECONDS = 4.0            # s, duration of each heat phase
    MEASURE_SECONDS = 1.0         # s, duration of each measure phase
    MEASURE_SETTLE_SECONDS = 0.5  # s, wait after dropping to excitation before sampling

    # Transient rejection by median sampling: at the end of the settle window we
    # take a few quick raw V/I samples and use their MEDIAN. A one-off settling
    # transient (unsettled PSU readback after a current change) shows up as an
    # outlier that the median discards, while the true value is the majority. No
    # history or reference value is needed, so there is no stale-reference risk.
    #
    # NOTE on loop cost: each raw sample is two blocking serial round-trips to
    # the PSU, and these run inside the synchronous main loop (ahead of the VNA
    # sweep). Keep MEASURE_SAMPLES small and SAMPLE_SPACING_S at/near zero to
    # avoid slowing the VNA cadence. 3 samples with no spacing still rejects a
    # single isolated outlier. These are overridable from config.
    MEASURE_SAMPLES = 3           # raw samples per measurement (odd number)
    SAMPLE_SPACING_S = 0.0        # s between raw samples (0 = back-to-back)

    # Physical plausibility: a real element cannot lose this many degrees in one
    # cycle. A larger apparent drop is a bad reading and must never be allowed to
    # increase the heating current.
    IMPLAUSIBLE_DROP_DT = 20.0    # degC per cycle

    LOOP_PERIOD = 5.0             # s, nominal full heat+measure cycle (for reference)
    SAFETY_MAX_TEMP = 130.0       # degC, hard overtemp cutoff

    def __init__(
        self,
        psu,
        steps=(65.0, 80.0, 95.0, 110.0, 120.0),
        dwell_seconds: float = 600.0,
        tolerance: float = 1.0,
        stable_samples: int = 3,
        # kp/ki/kd kept in the signature for config/backward compatibility; unused now.
        kp: float = 0.0020, ki: float = 0.0010, kd: float = 0.0000,
        # Measurement-cost knobs (None = use class defaults). These directly
        # affect how long each measure cycle blocks the main loop before the VNA
        # sweep, so they are exposed for tuning VNA cadence vs. noise rejection.
        measure_samples: int | None = None,
        sample_spacing_s: float | None = None,
        measure_settle_seconds: float | None = None,
        log_writer=None, log_file=None,
    ):
        self.psu = psu
        self.steps = list(steps)
        self.dwell_seconds = float(dwell_seconds)
        self.tolerance = float(tolerance)
        self.stable_samples = int(stable_samples)
        self._log_writer = log_writer
        self._log_file = log_file

        # Override measurement-cost knobs if provided (else keep class defaults).
        if measure_samples is not None:
            self.MEASURE_SAMPLES = int(measure_samples)
        if sample_spacing_s is not None:
            self.SAMPLE_SPACING_S = float(sample_spacing_s)
        if measure_settle_seconds is not None:
            self.MEASURE_SETTLE_SECONDS = float(measure_settle_seconds)

        # Heating-current ramp state
        self._heat_current = self.MIN_HEATER_CURRENT
        self._last_temp = None          # most recent valid measurement (degC)
        self._prev_temp = None          # measurement from the previous cycle (degC)
        self._last_resistance = None
        self._last_meas_current = self.EXCITATION_CURRENT
        self._last_meas_volts = None
        self._cycle_count = 0

        # Phase machine state
        self._phase = "heat"            # "heat" or "measure"
        self._phase_started = None      # wall-clock monotonic timestamp
        self._measure_sampled = False   # whether we've taken this cycle's reading

        # Setpoint schedule state (unchanged semantics)
        self.step_idx = 0
        self.arrived = False
        self.stable_count = 0
        self.dwell_start = None

    # ------------------------------------------------------------------ setup
    def initialize_hardware(self):
        self.psu.set_voltage(self.CHANNEL, self.DRIVE_VOLTAGE_LIMIT)
        self.psu.set_current(self.CHANNEL, self._heat_current)
        self.psu.output_on()
        self._phase = "heat"
        self._phase_started = time.monotonic()
        self._measure_sampled = False

    # ------------------------------------------------------------ measurement
    def _read_once_at(self, excitation_current: float):
        """One raw V/I reading -> (temperature, resistance, amps, volts)."""
        volts = self.psu.measure_voltage(self.CHANNEL)
        amps = self.psu.measure_current(self.CHANNEL)
        # Fall back to the commanded current if the readback is too small to trust.
        if amps < 0.0005:
            amps = excitation_current
        resistance = volts / amps if amps > 0 else 0.0
        temperature = resistance_to_temperature(resistance)
        return temperature, resistance, amps, volts

    def _read_temperature_at(self, excitation_current: float):
        """Median-filtered V/I measurement.

        Takes MEASURE_SAMPLES raw readings and returns the sample whose
        resistance is the median. Using the median of raw samples rejects an
        isolated settling transient at the source, so a single bad readback can
        never drive the control loop. Returns (temperature, resistance, amps,
        volts) for the chosen (median) sample.
        """
        samples = []
        for i in range(self.MEASURE_SAMPLES):
            samples.append(self._read_once_at(excitation_current))
            if i < self.MEASURE_SAMPLES - 1:
                time.sleep(self.SAMPLE_SPACING_S)
        # Pick the sample at the median resistance (robust to outliers).
        samples.sort(key=lambda s: s[1])
        median = samples[len(samples) // 2]
        return median

    def measure_temperature(self) -> tuple[float, float, float]:
        """
        Backward-compatible helper: returns (temp, resistance, current).
        Returns the last valid multiplexed measurement when available so callers
        never see a reading taken mid heat phase.
        """
        if self._last_temp is not None:
            return self._last_temp, self._last_resistance, self._last_meas_current
        temp, resistance, amps, _ = self._read_temperature_at(self.EXCITATION_CURRENT)
        return temp, resistance, amps

    # ----------------------------------------------------------- ramp control
    def _adjust_heat_current(self, temp: float, setpoint: float, delta_t) -> str:
        """Ramp heating current toward the setpoint; return a phase label.

        delta_t is the temperature change since the previous cycle (degC), or
        None on the first cycle. When below setpoint but the temperature is
        barely moving, the element has stalled at the current power level, so we
        keep raising current toward the cap instead of prematurely holding.
        """
        error = setpoint - temp

        # Physical-plausibility guard: a heated element with real thermal mass
        # cannot drop many degrees in a single ~5 s cycle. A large negative dT is
        # therefore a bad reading, not reality. Never let such a spike command
        # MORE current (the dangerous direction). Freeze current and wait for the
        # next median-filtered reading to confirm the true temperature.
        if delta_t is not None and delta_t <= -self.IMPLAUSIBLE_DROP_DT:
            return f"ramp {self.step_idx + 1} (implausible drop dT={delta_t:+.1f}, I frozen)"

        if error > self.tolerance:
            # If the temperature is still climbing hard from the last push, hold
            # current steady for a cycle and let the thermal mass catch up. This
            # prevents stacking current faster than the element responds, which
            # is what causes overshoot past the setpoint.
            if delta_t is not None and delta_t >= self.FAST_RISE_DELTA_T:
                return f"ramp {self.step_idx + 1} (settle, dT={delta_t:+.1f})"

            stalled = delta_t is not None and delta_t < self.STALL_DELTA_T

            # Proportional step: big increments when far from target, shrinking
            # to a small floor as we approach so we decelerate into the band.
            frac = min(1.0, error / self.PROPORTIONAL_BAND)
            step = max(self.MIN_CURRENT_STEP, self.CURRENT_STEP * frac)
            self._heat_current = min(self._heat_current + step, self.MAX_HEATER_CURRENT)

            at_cap = self._heat_current >= self.MAX_HEATER_CURRENT
            if at_cap:
                return f"ramp {self.step_idx + 1} (I at cap {self.MAX_HEATER_CURRENT*1000:.0f}mA)"
            if stalled:
                return f"ramp {self.step_idx + 1} (stall-push)"
            return f"ramp {self.step_idx + 1}"
        elif error < -self.tolerance:
            # Above target: back off.
            self._heat_current = max(
                self._heat_current - self.CURRENT_STEP, self.MIN_HEATER_CURRENT
            )
            return f"cool {self.step_idx + 1}"
        else:
            # Inside the band: hold.
            return f"hold {self.step_idx + 1}"

    def _advance_schedule(self, temp: float, setpoint: float, t_now: float):
        """Stepped setpoint + dwell state machine (unchanged semantics)."""
        error = setpoint - temp
        if not self.arrived:
            if abs(error) <= self.tolerance:
                self.stable_count += 1
                if self.stable_count >= self.stable_samples:
                    self.arrived = True
                    self.dwell_start = t_now
                    logger.info(
                        f"Step {self.step_idx + 1} reached ({setpoint}degC). "
                        f"Dwelling {self.dwell_seconds}s."
                    )
            else:
                self.stable_count = 0
        else:
            remaining = self.dwell_seconds - (t_now - self.dwell_start)
            if remaining <= 0 and self.step_idx + 1 < len(self.steps):
                self.step_idx += 1
                self.arrived = False
                self.stable_count = 0
                self.dwell_start = None
                logger.info(f"Advancing to step {self.step_idx + 1} ({self.steps[self.step_idx]}degC).")

    # ------------------------------------------------------------------- tick
    def update_tick(self, t_now: float) -> dict:
        """
        Non-blocking single cycle. Call as often as the main loop runs; phase
        transitions are driven off wall-clock time, not call count.
        """
        now = time.monotonic()
        if self._phase_started is None:
            self._phase_started = now
        elapsed = now - self._phase_started
        setpoint = self.steps[self.step_idx]

        # ---------------------------------------------------- MEASURE phase
        if self._phase == "measure":
            # Command the small excitation current the moment we enter the phase.
            if not self._measure_sampled and elapsed < self.MEASURE_SETTLE_SECONDS:
                self.psu.set_current(self.CHANNEL, self.EXCITATION_CURRENT)
                return self._status(setpoint, phase=f"measure-settle {self.step_idx + 1}")

            # After settling, take exactly one reading for this cycle.
            if not self._measure_sampled:
                temp, resistance, amps, volts = self._read_temperature_at(self.EXCITATION_CURRENT)
                delta_t = None if self._last_temp is None else (temp - self._last_temp)
                self._measure_sampled = True
                self._cycle_count += 1

                # Safety first: honor an over-temperature reading even if it also
                # looks like a glitch. A false trip here is the safe failure mode.
                if temp >= self.SAFETY_MAX_TEMP:
                    logger.error(
                        f"SAFETY CUTOFF: {temp:.2f}degC exceeds limit. Clamping to excitation current."
                    )
                    self._prev_temp = self._last_temp
                    self._last_temp = temp
                    self._last_resistance = resistance
                    self._last_meas_current = amps
                    self._last_meas_volts = volts
                    self._heat_current = self.MIN_HEATER_CURRENT
                    self.psu.set_current(self.CHANNEL, self.EXCITATION_CURRENT)
                    return self._status(setpoint, phase="OVERTEMP")

                # The reading is median-filtered at the source, so transients are
                # already rejected. Accept it directly.
                self._prev_temp = self._last_temp
                self._last_temp = temp
                self._last_resistance = resistance
                self._last_meas_current = amps
                self._last_meas_volts = volts

                # Decide next heat current and advance the step schedule.
                phase_label = self._adjust_heat_current(temp, setpoint, delta_t)
                self._advance_schedule(temp, setpoint, t_now)
                self._pending_phase_label = phase_label

                # Rich per-cycle tuning line (one per measure cycle).
                heat_power = self._heat_current ** 2 * resistance
                dt_str = "  n/a" if delta_t is None else f"{delta_t:+5.2f}"
                logger.info(
                    f"[RTD #{self._cycle_count:04d}] "
                    f"T={temp:6.2f}degC  set={setpoint:5.1f}  err={setpoint - temp:+6.2f}  "
                    f"dT={dt_str}degC/cyc  |  "
                    f"I_heat={self._heat_current*1000:5.1f}mA  P={heat_power:5.2f}W  "
                    f"R={resistance:6.2f}ohm  Vexc={volts:5.3f}V  |  {phase_label}"
                )

            # Stay in measure until the window elapses, then switch to heat.
            if elapsed >= self.MEASURE_SECONDS:
                self._phase = "heat"
                self._phase_started = now
                self.psu.set_current(self.CHANNEL, self._heat_current)

            return self._status(
                setpoint, phase=getattr(self, "_pending_phase_label", f"ramp {self.step_idx + 1}")
            )

        # ------------------------------------------------------- HEAT phase
        # Ensure the commanded heating current is applied.
        self.psu.set_current(self.CHANNEL, self._heat_current)
        if elapsed >= self.HEAT_SECONDS:
            self._phase = "measure"
            self._phase_started = now
            self._measure_sampled = False

        return self._status(setpoint, phase=f"heat {self.step_idx + 1}")

    # ----------------------------------------------------------------- status
    def _status(self, setpoint: float, phase: str) -> dict:
        """Build the return dict using the last valid measurement."""
        temp = self._last_temp
        resistance = self._last_resistance
        error = (setpoint - temp) if temp is not None else None
        return {
            "setpoint": setpoint,
            "temp": temp,
            "resistance": resistance,
            "amps": self._heat_current,          # commanded heating current
            "volts": self.DRIVE_VOLTAGE_LIMIT,   # compliance ceiling
            "error": error,
            "phase": phase,
        }
