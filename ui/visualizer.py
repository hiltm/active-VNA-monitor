# ui/visualizer.py
import matplotlib.pyplot as plt
import numpy as np
from core.models import MeasurementRecord

class LivePlotter:
    def __init__(self, start_freq: float, stop_freq: float):
        plt.ion()
        self.fig, self.ax = plt.subplots(figsize=(8, 5))
        self.start_freq = start_freq
        self.stop_freq = stop_freq

    def update(self, record: MeasurementRecord):
        self.ax.clear()
        mag_s21 = 20 * np.log10(np.abs(record.s21) + 1e-12)
        mag_s11 = 20 * np.log10(np.abs(record.s11) + 1e-12)

        self.ax.plot(record.freqs, mag_s21, label='S21')
        self.ax.plot(record.freqs, mag_s11, label='S11')
        self.ax.set_xlabel("Frequency (Hz)")
        self.ax.set_ylabel("Magnitude (dB)")
        title = f"Frequency Response | {record.timestamp_str}"
        if record.temperature:
            title += f" | Temp: {record.temperature:.1f}°C"
        self.ax.set_title(title)
        self.ax.set_xlim(self.start_freq, self.stop_freq)
        self.ax.set_ylim(-60, 0)
        self.ax.legend()
        self.ax.grid(True)

        self.fig.canvas.draw()
        self.fig.canvas.flush_events()
        plt.pause(0.01)

    def close(self):
        plt.ioff()
        plt.close(self.fig)
