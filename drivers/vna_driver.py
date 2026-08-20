# drivers/vna_driver.py
import numpy as np
import pynanovna
import logging
import time

logger = logging.getLogger(__name__)

class VNADevice:
    def __init__(self, start_freq: int, stop_freq: int, points: int):
        self.start_freq = int(start_freq)
        self.stop_freq = int(stop_freq)
        self.points = int(points)
        self.vna = None

    def connect(self):
        self.vna = pynanovna.VNA()
        self.vna.set_sweep(self.start_freq, self.stop_freq, self.points)

    def sweep(self, retries: int = 3, delay: float = 1.0):
        for attempt in range(retries):
            try:
                s11, s21, freqs = self.vna.sweep()
                s11 = np.array(s11, dtype=complex)
                s21 = np.array(s21, dtype=complex)
                freqs = np.array(freqs)
                s12 = np.zeros_like(s11)
                s22 = np.zeros_like(s21)
                return freqs, s11, s21, s12, s22
            except Exception as e:
                logger.warning(f"VNA sweep attempt {attempt+1} failed: {e}")
                time.sleep(delay)
        raise RuntimeError("VNA failed to complete sweep after retries.")

    def close(self):
        if hasattr(self.vna, 'close'):
            self.vna.close()
