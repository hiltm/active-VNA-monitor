# core/storage.py
import os
import shutil
import logging
from pathlib import Path
from datetime import datetime
import numpy as np
import skrf as rf
from core.models import MeasurementRecord

logger = logging.getLogger("VNASuite")


class DataStorage:
    """
    Resilient Touchstone writer.

    The primary save location (``base_dir/experiment_name``) is the source of
    truth. It is typically a network mount, which can briefly disappear
    (e.g. OSError Errno 112 "Host is down", or a stale NFS handle). To avoid
    crashing the acquisition run and to avoid losing data:

      * If the primary location is unreachable, records are spooled to a LOCAL
        fallback directory and the storage enters a "degraded" state.
      * While degraded, every save first re-probes the primary. As soon as the
        primary is reachable again, all spooled files are moved over to it and
        writing resumes directly to the primary ("self-healing").

    A save never raises on I/O failure; it returns the Path actually written, or
    None if neither primary nor fallback could be written (the caller keeps the
    acquisition loop alive either way).
    """

    def __init__(
        self,
        base_dir: Path | str,
        experiment_name: str,
        fallback_base_dir: Path | str | None = None,
    ):
        self.experiment_name = experiment_name
        self.primary_dir = Path(base_dir) / experiment_name

        # Local spool directory, used only when the primary is unreachable.
        if fallback_base_dir is None:
            fallback_base_dir = Path.home() / "vna_fallback_data"
        self.fallback_dir = Path(fallback_base_dir) / experiment_name

        self._degraded = False  # True while writing to the local spool

        # Try to prepare the primary; if it is down at startup, begin degraded.
        if self._ensure_dir(self.primary_dir):
            logger.info(f"Primary storage ready at {self.primary_dir}")
        else:
            self._enter_degraded("primary storage unavailable at startup")

    # ------------------------------------------------------------- directory
    @staticmethod
    def _ensure_dir(path: Path) -> bool:
        """Return True iff `path` exists (or can be created) and is writable.

        A tiny probe file is written and removed to detect mounts that accept
        mkdir but fail on actual I/O (and dead/stale network handles).
        """
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_probe"
            with open(probe, "w") as f:
                f.write("ok")
            probe.unlink()
            return True
        except (PermissionError, OSError) as e:
            logger.debug(f"Directory {path} not writable: {e}")
            return False

    def _enter_degraded(self, reason: str):
        if not self._degraded:
            logger.warning(
                f"Storage DEGRADED ({reason}). Spooling records locally to "
                f"{self.fallback_dir}; will auto-migrate when primary returns."
            )
        self._degraded = True
        # Make sure the local spool exists; if even this fails we are in trouble,
        # but save_record will surface that per-write.
        try:
            self.fallback_dir.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError) as e:
            logger.error(f"Local fallback dir {self.fallback_dir} also unwritable: {e}")

    # ------------------------------------------------------------- self-heal
    def _try_recover_primary(self):
        """If degraded and the primary is reachable again, migrate spooled files."""
        if not self._degraded:
            return
        if not self._ensure_dir(self.primary_dir):
            return  # still down

        moved, failed = 0, 0
        for src in sorted(self.fallback_dir.glob("*.s2p")):
            dst = self.primary_dir / src.name
            # Avoid clobbering if a same-named file somehow exists on the primary.
            if dst.exists():
                dst = self.primary_dir / f"{src.stem}_recovered_{datetime.now():%H%M%S_%f}{src.suffix}"
            try:
                shutil.move(str(src), str(dst))
                moved += 1
            except (PermissionError, OSError) as e:
                failed += 1
                logger.warning(f"Could not migrate spooled {src.name}: {e}")

        if failed == 0:
            self._degraded = False
            logger.info(
                f">>> Primary storage RESTORED. Migrated {moved} spooled file(s) to "
                f"{self.primary_dir}. Resuming direct writes. <<<"
            )
        else:
            logger.warning(
                f"Primary reachable but {failed} spooled file(s) failed to migrate; "
                f"staying degraded and will retry next cycle."
            )

    # ----------------------------------------------------------------- write
    def _build_network(self, record: MeasurementRecord) -> rf.Network:
        N = len(record.freqs)
        s = np.zeros((N, 2, 2), dtype=complex)
        s[:, 0, 0] = record.s11  # S11 (Port 1 reflection)
        s[:, 0, 1] = record.s12  # S12 (Reverse transmission)
        s[:, 1, 0] = record.s21  # S21 (Forward transmission)
        s[:, 1, 1] = record.s22  # S22 (Port 2 reflection)
        freq_obj = rf.Frequency.from_f(record.freqs, unit='Hz')
        return rf.Network(frequency=freq_obj, s=s, name="NanoVNA")

    def save_record(self, record: MeasurementRecord, subfolder: str = None) -> Path | None:
        """
        Save a Touchstone (.s2p) file.

        Writes to the primary location when healthy; otherwise spools to the
        local fallback. Before each degraded write, attempts to recover the
        primary and migrate any backlog. Never raises on I/O failure: returns
        the Path written, or None if nothing could be written.
        """
        # Opportunistically self-heal before deciding where to write.
        self._try_recover_primary()

        ntwk = self._build_network(record)
        base_filename = f"measurement_{record.timestamp_str}.s2p"

        # Choose target: primary if healthy, else the local spool.
        if not self._degraded:
            target_dir = self.primary_dir if not subfolder else self.primary_dir / subfolder
            if not self._ensure_dir(target_dir):
                self._enter_degraded("primary became unreachable during run")

        if self._degraded:
            target_dir = self.fallback_dir if not subfolder else self.fallback_dir / subfolder
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except (PermissionError, OSError) as e:
                logger.error(f"Both primary and fallback unwritable, dropping record: {e}")
                return None

        return self._write_with_retry(ntwk, target_dir, base_filename)

    def _write_with_retry(self, ntwk: rf.Network, target_dir: Path, base_filename: str) -> Path | None:
        """Write, retrying under a fresh timestamped name on transient errors."""
        target_path = target_dir / base_filename
        for attempt in range(3):
            try:
                ntwk.write_touchstone(str(target_path))
                return target_path
            except (PermissionError, OSError) as e:
                ts_alt = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                target_path = target_dir / f"measurement_{ts_alt}.s2p"
                logger.warning(
                    f"Could not write {base_filename} ({e}). "
                    f"Retry {attempt + 1}/3 as {target_path.name}"
                )
        # If we were writing to the primary and it just failed, flip to degraded
        # so the next cycle spools locally instead of failing again.
        if target_dir == self.primary_dir or target_dir.is_relative_to(self.primary_dir):
            self._enter_degraded("primary writes failing")
        logger.error(f"Failed to write Touchstone after retries into {target_dir}.")
        return None
