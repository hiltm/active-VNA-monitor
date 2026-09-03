# targets/Z80/Z80_driver.py
import os
import sys
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
        binary_path: str = "targets/Z80/Test1Sec_ND.bin",
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
            time.sleep(1.0)  # Allow port and board logic to settle
            logger.info(f"Connected to Z80 on {self.port} @ {self.baud_rate} baud")

    def disconnect(self):
        """Stops monitoring and closes serial handle."""
        self.stop_monitoring()
        if self._ser and self._ser.is_open:
            self._ser.close()
            logger.info("Closed Z80 serial connection.")
        self._ser = None

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

    def send_kickoff(self):
        """Sends the kickoff byte (default 0x05) to release the Z80 execution loop."""
        if self.kickoff_byte is not None and self._ser and self._ser.is_open:
            try:
                self._ser.write(bytes([self.kickoff_byte]))
                logger.info(f"Sent kickoff byte (0x{self.kickoff_byte:02x}) to Z80.")
                if self.response_file:
                    ts = datetime.datetime.now().strftime("%d-%b-%Y %H:%M:%S.%f")
                    with open(self.response_file, 'a') as f:
                        f.write(f"Time: {ts} Data: Sent: {self.kickoff_byte:02x}\n")
            except Exception as e:
                logger.error(f"Failed sending kickoff byte: {e}")

    def verify_heartbeat(self, count: int = 4, timeout_s: float = 6.0) -> bool:
        """
        Listens for the periodic Z80 heartbeat (0x01).
        Returns True once `count` occurrences of 0x01 are confirmed, False on timeout.
        """
        if self._ser is None or not self._ser.is_open:
            logger.error("Cannot verify heartbeat: port is closed.")
            return False

        logger.info(f"Listening for {count} sync bytes (0x01) within {timeout_s:.1f}s...")
        received_ones = 0
        deadline = time.time() + timeout_s

        while time.time() < deadline:
            if self._ser.in_waiting > 0:
                chunk = self._ser.read(self._ser.in_waiting)
                for b in chunk:
                    if b == 0x01:
                        received_ones += 1
                        logger.info(f"Received sync byte ({received_ones}/{count}): 0x01")
                        if received_ones >= count:
                            return True
                    else:
                        logger.debug(f"Received other byte: 0x{b:02x}")
            time.sleep(0.05)

        logger.warning(f"Sync check incomplete: received {received_ones}/{count} sync bytes.")
        return False

    def start_monitoring(self, response_file_path: Path):
        """Spins up a background worker to stream raw Z80 traffic to disk."""
        self.response_file = Path(response_file_path)
        self.response_file.parent.mkdir(parents=True, exist_ok=True)
        self._stop_monitor.clear()

        # Issue kickoff byte
        self.send_kickoff()

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


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )

    parser = argparse.ArgumentParser(description="Standalone Z80 Controller Test")
    parser.add_argument("--port", type=str, default="/dev/ttyUSB0", help="Serial port (default: /dev/ttyUSB0)")
    parser.add_argument("--baud", type=int, default=9600, help="Baud rate (default: 9600)")
    parser.add_argument(
        "--bin", 
        type=str, 
        default=str(Path(__file__).parent / "Test1Sec_ND.bin"),
        help="Path to Z80 binary payload"
    )
    parser.add_argument("--skip-bload", action="store_true", help="Skip BLOAD and only monitor serial traffic")
    args = parser.parse_args()

    print(f"Testing Z80 controller on {args.port} @ {args.baud} baud...")
    z80 = Z80Controller(
        port=args.port,
        baud_rate=args.baud,
        binary_path=args.bin,
        binary_addr="0800",
        kickoff_byte=5
    )

    try:
        z80.connect()

        if not args.skip_bload:
            print("\n--- Starting BLOAD Transfer ---")
            success = z80.program_and_run()
            if not success:
                print("[ERROR] BLOAD programming failed.")
                sys.exit(1)
            print("[SUCCESS] BLOAD finished and RUN issued.")

        # Heartbeat verification
        z80.send_kickoff()
        print("\n--- Verifying Z80 Heartbeat (01 01 01 01) ---")
        if z80.verify_heartbeat(count=4, timeout_s=6.0):
            print("[SUCCESS] Z80 verified active and transmitting heartbeats!")
        else:
            print("[WARNING] Did not observe 4 heartbeat bytes within timeout.")

        print("\n--- Monitoring Z80 Responses (Press Ctrl+C to stop) ---")
        log_file = Path("z80_debug_traffic.txt")
        z80.start_monitoring(log_file)

        while True:
            time.sleep(0.5)
            if log_file.exists():
                with open(log_file, "r") as f:
                    lines = f.readlines()
                    if lines:
                        last_line = lines[-1].strip()
                        print(f"Latest traffic: {last_line}")

    except KeyboardInterrupt:
        print("\n\nTest stopped by user.")
    finally:
        z80.disconnect()
        print("Z80 port closed.")