# targets/z80/z80_driver.py
import os
import time
import serial
import binascii
import logging
import datetime
import threading
from pathlib import Path

logger = logging.getLogger("VNASuite")

class Z80Controller:
    """Headless driver for Z80 Single Board Computer (BLOAD, execution, and traffic logging)."""

    def __init__(
        self,
        port: str,
        baud_rate: int = 9600,
        binary_path: str = "targets/z80/firmware/test_payload.bin",
        binary_addr: str = "0800",
        kickoff_byte: int = 5,
        step_timeout_s: float = 5.0,
        settle_s: float = 0.6
    ):
        self.port = port
        self.baud_rate = baud_rate
        self.binary_path = Path(binary_path)
        self.binary_addr = binary_addr
        self.kickoff_byte = kickoff_byte
        self.step_timeout_s = step_timeout_s
        self.settle_s = settle_s

        self._ser = None
        self._monitor_thread = None
        self._stop_monitor = threading.Event()
        self.response_file = None

    def connect(self):
        """Opens the serial port to the Z80."""
        if self._ser is None or not self._ser.is_open:
            self._ser = serial.Serial(self.port, self.baud_rate, timeout=1.0)
            time.sleep(1.0)  # Allow port and board to settle
            logger.info(f"Connected to Z80 on {self.port} @ {self.baud_rate} baud")

    def disconnect(self):
        """Stops monitoring and closes serial handle."""
        self.stop_monitoring()
        if self._ser and self._ser.is_open:
            self._ser.close()
            logger.info("Closed Z80 serial connection.")
        self._ser = None

    # Signal that indicates the Z80/DUT is working correctly.
    # After the board is reset it emits a stream of 0x01 bytes
    # (e.g. 01 01 01) on the serial link. Seeing this pattern means the
    # DUT is alive and communicating as expected.
    VALID_COMMS_SIGNAL = bytes([0x01, 0x01, 0x01])

    def check_serial_comms(self, listen_s: float | None = None) -> bool:
        """Passively verify the Z80/DUT serial link is producing valid traffic.

        The working DUT emits a stream of 0x01 bytes (e.g. 01 01 01) after a
        reset. This method just listens on the port (it does NOT send any
        command) and reports whether that expected 0x01 traffic is seen.

        Returns True when valid 0x01 traffic is observed, False otherwise.
        Intended as an optional aid; the operator visually confirms traffic
        after pressing the Z80 reset button.
        """
        try:
            self.connect()
        except Exception as e:
            logger.error(f"Z80 comms check: could not open serial port {self.port}: {e}")
            return False

        window = listen_s if listen_s is not None else self.step_timeout_s
        accumulated = b''
        deadline = time.time() + window
        while time.time() < deadline:
            try:
                if self._ser and self._ser.in_waiting > 0:
                    accumulated += self._ser.read_all()
            except Exception as e:
                logger.error(f"Z80 comms check: read error: {e}")
                return False
            time.sleep(0.05)

        if not accumulated:
            logger.warning("Z80 comms check: no serial traffic seen (link may be idle or dead).")
            return False

        # Expected valid traffic: 0x01 bytes (the 01 01 01 pattern).
        if any(b == 0x01 for b in accumulated):
            logger.info(
                f"Z80 comms check: valid 0x01 traffic observed "
                f"({' '.join(f'{b:02x}' for b in accumulated[:8])}...). DUT is communicating."
            )
            return True

        logger.warning(
            f"Z80 comms check: traffic seen but no expected 0x01 bytes "
            f"({' '.join(f'{b:02x}' for b in accumulated[:8])}...)."
        )
        return False

    def _write_command(self, cmd_text: str):
        """Writes a line command terminated with CR."""
        if self._ser:
            self._ser.write((cmd_text + '\r').encode('utf-8'))

    def _wait_for_response(self) -> bytes:
        """Polls for serial response with settle window."""
        accumulated = b''
        deadline = time.time() + self.step_timeout_s
        while time.time() < deadline:
            if self._ser and self._ser.in_waiting > 0:
                accumulated += self._ser.read_all()
                quiet_since = time.time()
                while time.time() - quiet_since < self.settle_s:
                    time.sleep(0.05)
                    if self._ser.in_waiting > 0:
                        accumulated += self._ser.read_all()
                        quiet_since = time.time()
                return accumulated
            time.sleep(0.05)
        return accumulated

    def _step(self, cmd_text: str, label: str) -> bytes:
        self._write_command(cmd_text)
        resp = self._wait_for_response()
        logger.debug(f"[Z80 {label}] Resp: {resp}")
        return resp

    def program_and_run(self) -> bool:
        """Uploads the binary payload via BLOAD and issues the RUN command."""
        if not self.binary_path.exists():
            logger.error(f"Binary file not found at {self.binary_path}")
            return False

        file_size = os.path.getsize(self.binary_path)
        if file_size == 0:
            logger.error("Binary file is empty.")
            return False

        self.connect()

        try:
            logger.info("Checking Z80 prompt with 'help'...")
            resp = self._step('help', 'help')
            if b'Commands implemented' not in resp:
                logger.warning("Z80 prompt did not echo expected help text. Proceeding with BLOAD anyway...")

            logger.info(f"Initiating BLOAD ({file_size} bytes to address {self.binary_addr})...")
            self._step('bload', 'bload')
            self._step(self.binary_addr, 'address')
            self._step(str(file_size), 'size')

            logger.info("Transmitting binary payload...")
            with open(self.binary_path, 'rb') as f:
                content = f.read()

            for i in range(len(content)):
                self._ser.write(content[i:i + 1])
                time.sleep(0.005)  # Pace transmission to prevent buffer overrun

            self._ser.write(b'\r')
            self._wait_for_response()

            logger.info(f"Issuing RUN from address {self.binary_addr}...")
            self._step('run', 'run')
            self._step(self.binary_addr, 'run-address')

            logger.info("Z80 binary upload complete and RUN command sent.")
            return True

        except Exception as e:
            logger.error(f"Error programming Z80: {e}")
            return False

    def start_monitoring(self, response_file_path: Path):
        """Spins up a background worker to stream raw Z80 traffic to disk."""
        self.response_file = Path(response_file_path)
        self.response_file.parent.mkdir(parents=True, exist_ok=True)
        self._stop_monitor.clear()

        # Send kickoff byte to start execution loop
        if self.kickoff_byte is not None and self._ser:
            try:
                self._ser.write(bytes([self.kickoff_byte]))
                ts = datetime.datetime.now().strftime("%d-%b-%Y %H:%M:%S.%f")
                with open(self.response_file, 'a') as f:
                    f.write(f"Time: {ts} Data: Sent: {self.kickoff_byte:02x}\n")
                logger.info(f"Sent kickoff byte (0x{self.kickoff_byte:02x}) to Z80.")
            except Exception as e:
                logger.error(f"Failed sending kickoff byte: {e}")

        def _monitor_worker():
            logger.info("Z80 traffic monitor thread active.")
            while not self._stop_monitor.is_set():
                try:
                    if self._ser and self._ser.in_waiting > 0:
                        raw = self._ser.read_all()
                        if raw:
                            ts = datetime.datetime.now().strftime("%d-%b-%Y %H:%M:%S.%f")
                            lines = [f"Time: {ts} Data: Received: {b:02x}\n" for b in raw]
                            with open(self.response_file, 'a') as f:
                                f.writelines(lines)
                    time.sleep(0.05)
                except Exception as err:
                    logger.error(f"Z80 monitoring error: {err}")
                    break

        self._monitor_thread = threading.Thread(target=_monitor_worker, daemon=True)
        self._monitor_thread.start()

    def stop_monitoring(self):
        """Stops the response logging thread."""
        self._stop_monitor.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2.0)
        self._monitor_thread = None