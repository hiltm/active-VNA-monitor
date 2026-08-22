# active-VNA-monitor

A hardware-in-the-loop acquisition suite for **RF reflectometry board-aging / tamper monitoring**. It continuously sweeps a nanoVNA against a device under test (DUT), optionally drives the DUT power and a PT100 RTD heating element through a programmable dual-channel supply, saves each sweep as a Touchstone `.s2p` file, and runs a machine-learning classifier that estimates whether the board is *fresh*, *lightly aged*, or *heavily aged* in real time.

The whole experiment runs as a single long-lived acquisition loop with automatic hardware-recovery, resilient logging, and a live plot.

---

## Table of Contents

- [System Overview](#system-overview)
- [Hardware Requirements](#hardware-requirements)
- [Software Requirements](#software-requirements)
- [Repository Layout](#repository-layout)
- [Configuration](#configuration)
- [Running the Experiment](#running-the-experiment)
- [Auto-Capture as a systemd Service](#auto-capture-as-a-systemd-service)
- [Detailed Operation of the Acquisition Loop](#detailed-operation-of-the-acquisition-loop)
- [Thermal Stepped-PID Control](#thermal-stepped-pid-control)
- [Machine-Learning Aging Classifier](#machine-learning-aging-classifier)
- [Blockchain Provenance Ledger](#blockchain-provenance-ledger)
- [Output Files](#output-files)
- [Fault Tolerance and Recovery](#fault-tolerance-and-recovery)
- [Stopping the Experiment](#stopping-the-experiment)
- [Troubleshooting](#troubleshooting)

---

## System Overview

`main.py` instantiates an `ExperimentController` from a single `AppConfig` dataclass and then:

1. Connects to every enabled subsystem (VNA, ambient sensor, PSU, DUT power, RTD heater, ML model, live plot).
2. Enters an infinite acquisition loop that, once per `loop_interval_seconds`:
   - reads the ambient temperature sensor,
   - advances the RTD heater's stepped-PID thermal profile,
   - reads DUT power telemetry,
   - performs a full VNA frequency sweep,
   - packs everything into a `MeasurementRecord`,
   - writes a Touchstone `.s2p` file and its SHA-256 checksum,
   - runs the ML aging classifier on the sweep,
   - appends a synchronized row to the telemetry CSV,
   - updates the live plot.
3. On any hardware communication failure, enters a blocking recovery loop that logs the outage, disconnects everything, and polls until the instruments are reachable again — then resumes automatically.
4. On `Ctrl+C`, safely powers down outputs, releases serial ports, and closes the plot.

The design goal is *unattended multi-day operation*: the loop tolerates power cycles, permission/lock errors on output files, and transient VNA sweep failures without losing the run.

---

## Hardware Requirements

| Subsystem | Device | Notes |
|-----------|--------|-------|
| VNA | nanoVNA (e.g. nanoVNA-F V2) | Driven via `pynanovna`. Provides S11 and S21; S12/S22 are zero-filled. |
| Power Supply | GW Instek **GPD-2303S** dual-channel PSU | 9600 8N1 ASCII over serial. CH1 = RTD heater, CH2 = DUT. |
| RTD Heating Element | PT100 sensor + resistive heater on PSU CH1 | Temperature inferred from V/I resistance via a PT100 lookup table. |
| DUT | Board under test, powered from PSU CH2 | Default 3.3 V, 0.5 A current limit. |
| Ambient Sensor | Optional serial temperature sensor | ASCII numeric readings, disabled by default. |

All serial devices are addressed by stable `/dev/serial/by-id/...` paths in the default config.

---

## Software Requirements

- Python 3.10+ (uses `float | None` union syntax and `:=` walrus operator).
- Python packages:
  - `pynanovna` — NanoVNA control
  - `scikit-rf` (`skrf`) — Touchstone `.s2p` writing
  - `numpy`
  - `scikit-learn` + `joblib` — ML model, scaler, metadata
  - `simple-pid` — RTD thermal control loop
  - `matplotlib` — live plotting
  - `pyserial` — PSU and ambient sensor serial I/O
  - `web3` — *optional*; only needed when `enable_blockchain_sync = True` (blockchain provenance ledger)

Install (example):

```bash
pip install pynanovna scikit-rf numpy scikit-learn joblib simple-pid matplotlib pyserial
```

The blockchain ledger is optional. Install `web3` only if you enable it:

```bash
pip install web3
```

> The suite imports `web3` lazily, so it runs normally without the package installed as long as `enable_blockchain_sync = False`.

---

## Repository Layout

```
active-VNA-monitor/
├── main.py                     # ExperimentController + acquisition loop (entry point)
├── config.py                   # AppConfig dataclass — all tunable parameters
├── core/
│   ├── models.py               # MeasurementRecord dataclass
│   ├── storage.py              # DataStorage — writes .s2p Touchstone files
│   └── logger.py               # Resilient event log + telemetry CSV + run manifest
├── drivers/
│   ├── vna_driver.py           # VNADevice — pynanovna wrapper with ret/reconnect logic
│   ├── gpd2303s.py             # GPD2303S — GW Instek PSU serial driver
│   ├── dut_power.py            # DUTPowerController — DUT supply on CH2
│   ├── rtd_heater.py           # RTDHeaterController — PT100 stepped-PID heater on CH1
│   └── temp_sensor.py          # TemperatureSensor — optional ambient serial sensor
├── plugins/
│   ├── ml_engine.py            # RFAgingClassifier — feature extraction + inference
│   ├── anomaly_detector.py     # (placeholder, empty)
│   └── blockchain_ledger.py    # BlockchainLedger — web3.py provenance ledger client
├── ui/
│   └── visualizer.py           # LivePlotter — live S11/S21 magnitude plot
├── models/
│   ├── rf_model.joblib         # Trained RandomForest classifier
│   ├── scaler.joblib           # StandardScaler fitted at training time
│   └── training_metadata.joblib
└── blockchain/                 # Minimal Hardhat project: DataRegistry contract + deploy
    ├── contracts/DataRegistry.sol
    ├── scripts/deploy.js        # writes artifacts_on_chain/contract-address.json
    ├── hardhat.config.js
    └── package.json             # compile / node / deploy scripts
```

---

## Configuration

All runtime behavior is controlled by the `AppConfig` dataclass in `config.py`. Edit it before running.

### Feature toggles

| Field | Default | Effect |
|-------|---------|--------|
| `enable_ambient_temp` | `False` | Connect and read the serial ambient temperature sensor. |
| `enable_power_supply` | `True` | Power the DUT on PSU CH2. |
| `enable_rtd_heater` | `True` | Run the stepped-PID RTD heater on PSU CH1. |
| `enable_live_plot` | `True` | Show the live matplotlib S11/S21 plot. |
| `enable_ml_evaluation` | `True` | Load the classifier and run inference each sweep. |
| `enable_anomaly_detection` | `False` | Reserved (placeholder plugin). |
| `enable_blockchain_sync` | `False` | Commit each sweep's SHA-256 + metadata to the local blockchain provenance ledger (see [Blockchain Provenance Ledger](#blockchain-provenance-ledger)). Requires `web3`. |

### Key parameters

| Field | Default | Meaning |
|-------|---------|---------|
| `loop_interval_seconds` | `1.0` | Sleep between acquisition cycles. |
| `base_save_dir` | `/home/chiptamper/chiptamper/data` | Root output directory. |
| `experiment_name` | `Z80_prelim_mod3_sn30` | Names the run subfolder and output files. |
| `start_freq` / `stop_freq` | `500e3` / `4e9` | VNA sweep range (Hz). |
| `points` | `201` | Number of sweep points (must match the model's expected feature length). |
| `ml_model_dir` | `.../models` | Directory containing the `.joblib` model, scaler, metadata. |
| `ml_model_type` | `rf` | Model prefix; loads `rf_model.joblib` (`knn`/`svm` also supported if present). |
| `vna_port`, `power_supply_port`, `ambient_temp_port` | serial paths | Device serial ports. |
| `dut_channel` / `dut_voltage` / `dut_current_limit` | `2` / `3.3` / `0.5` | DUT supply on CH2. |
| `rtd_steps` | `[65, 80, 95, 110, 120]` | Temperature setpoint schedule (°C). |
| `rtd_dwell_seconds` | `600.0` | Hold time at each step once stable. |
| `rtd_tolerance` | `1.0` | °C band considered "on setpoint". |
| `rtd_stable_samples` | `3` | Consecutive in-band samples before dwell starts. |
| `rtd_kp` / `rtd_ki` / `rtd_kd` | `0.0020` / `0.0010` / `0.0` | PID gains (output = heater current in A). |
| `blockchain_rpc_url` | `http://127.0.0.1:8545` | JSON-RPC endpoint of the local chain (used when `enable_blockchain_sync`). |
| `blockchain_contract_address_file` | `.../artifacts_on_chain/contract-address.json` | Path to the Hardhat deploy artifact holding the deployed `DataRegistry` address. |
| `blockchain_chain_id` | `None` | Chain id; `None` auto-detects from the node. |
| `blockchain_private_key` | `None` | Signing key; `None` uses a node-unlocked account (Hardhat default). |

> **Note:** `base_save_dir` and `ml_model_dir` default to absolute paths under `/home/chiptamper/...`. Update these for your machine before running.

---

## Running the Experiment

```bash
python main.py
```

There are no command-line arguments — configuration comes entirely from `config.py`. On start the controller retries hardware connection every 5 seconds until all enabled subsystems are online, then begins acquiring. Progress is printed to the console and written to the event log.

---

## Auto-Capture as a systemd Service

For unattended, boot-persistent operation the suite can run as a systemd service named **`active-RF-monitor.service`**. This lets the acquisition loop start automatically at boot and restart itself if it exits.

### One-time setup

Create the unit file at `/etc/systemd/system/active-RF-monitor.service`. Adjust `User`, `WorkingDirectory`, and the `python`/`ExecStart` paths to match your machine (use an absolute path to the interpreter, ideally a virtualenv):

```ini
[Unit]
Description=Active VNA RF Reflectometry Aging Monitor
After=network.target

[Service]
Type=simple
User=chiptamper
WorkingDirectory=/home/chiptamper/chiptamper/git/active-VNA-monitor
ExecStart=/usr/bin/python3 /home/chiptamper/chiptamper/git/active-VNA-monitor/main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> **Note:** the live matplotlib plot (`enable_live_plot`) needs a display. When running headless under systemd, set `enable_live_plot = False` in `config.py`, otherwise plotting will fail.

After creating or editing the unit file, reload systemd so it picks up the change:

```bash
sudo systemctl daemon-reload
```

### Enabling auto-capture (start on boot)

```bash
# Start now AND enable automatic start at every boot
sudo systemctl enable --now active-RF-monitor.service
```

To enable it for future boots without starting it immediately, drop `--now`:

```bash
sudo systemctl enable active-RF-monitor.service
```

### Disabling auto-capture

```bash
# Stop now AND prevent it from starting at boot
sudo systemctl disable --now active-RF-monitor.service
```

To disable auto-start on boot but leave the currently running instance alone, drop `--now`:

```bash
sudo systemctl disable active-RF-monitor.service
```

### Manual control and status

```bash
sudo systemctl start   active-RF-monitor.service   # start a run now
sudo systemctl stop    active-RF-monitor.service   # stop the running run (clean shutdown)
sudo systemctl restart active-RF-monitor.service   # restart the run

systemctl status  active-RF-monitor.service        # check whether it is active/enabled
systemctl is-enabled active-RF-monitor.service      # just the boot-enable state
```

Stopping the service sends `SIGTERM`, which the controller handles like `Ctrl+C`: it powers down PSU outputs, releases serial ports, and closes the plot before exiting.

### Viewing logs

The service's console output (everything the suite prints, including `[INFO]`/`[ERROR]` lines) is captured by the journal:

```bash
journalctl -u active-RF-monitor.service -f          # follow live
journalctl -u active-RF-monitor.service --since today
```

This is in addition to the suite's own `<experiment>_events.log` and telemetry CSV written under `base_save_dir/experiment_name/` (see [Output Files](#output-files)).

---

## Detailed Operation of the Acquisition Loop

Each iteration of the main loop (`ExperimentController.run`) performs the following ordered steps:

1. **Timestamps** — capture wall-clock epoch time, monotonic elapsed time, and a `datetime`.
2. **Ambient read** — `TemperatureSensor.read_temperature()` (if enabled) parses a numeric ASCII line, retrying up to 3 times and rejecting values below `min_valid_temp` (20 °C).
3. **Thermal tick** — `RTDHeaterController.update_tick()` measures heater temperature, applies the PID, and advances the step/dwell state machine (see below).
4. **DUT telemetry** — `DUTPowerController.read_telemetry()` reads CH2 voltage/current and computes power.
5. **VNA sweep** — `VNADevice.sweep()` returns `freqs, s11, s21, s12, s22`. S12 and S22 are zero-filled (2-port matrix completed for Touchstone compliance). Sweeps retry up to 3 times with a 1 s delay.
6. **Record packing** — all channels are bundled into a `MeasurementRecord` (temperature prefers the heater reading, falling back to ambient).
7. **Touchstone save** — `DataStorage.save_record()` builds a `scikit-rf` 2×2 S-parameter network and writes `measurement_<timestamp>.s2p`. A SHA-256 checksum of the file is computed for integrity.
8. **ML inference** — if enabled, `RFAgingClassifier.evaluate()` extracts a feature vector, scales it, predicts the aging class, and reports class id, label, confidence, and per-class probabilities.
9. **Blockchain commit** — if `enable_blockchain_sync` is set, `BlockchainLedger.record_measurement()` writes the sweep's SHA-256 + metadata to the on-chain `DataRegistry` contract and returns a transaction hash. This step is fail-soft: a chain error is logged and the loop continues (the tx column is left blank).
10. **Telemetry row** — a fully synchronized row (RF file + checksum + thermal + DUT + ML columns + blockchain tx) is appended to the telemetry CSV.
11. **Live plot** — `LivePlotter.update()` redraws S11 and S21 magnitude (dB) vs frequency with the current timestamp/temperature in the title.
12. **Sleep** — waits `loop_interval_seconds` before the next cycle.

If steps 2–5 raise (any hardware I/O error), the loop calls `wait_for_hardware_recovery()` and skips the rest of that iteration.

---

## Thermal Stepped-PID Control

The RTD heater (`drivers/rtd_heater.py`) uses PSU CH1 in a constant-voltage-limited / current-driven configuration:

- **Temperature sensing is indirect:** the PT100 resistance is derived from `V / I` measured on CH1, then converted to °C via linear interpolation of an embedded PT100 resistance table.
- **PID output is the heater current** (clamped between 50 mA floor and 100 mA safety cap; drive voltage limited to 20 V).
- **Step schedule state machine:**
  1. *Ramp* toward the current setpoint in `rtd_steps`.
  2. Once within `rtd_tolerance` for `rtd_stable_samples` consecutive ticks → *settle* → begin *dwell*.
  3. Hold for `rtd_dwell_seconds`, then advance to the next setpoint.
  4. After the last step → `hold-final`.
- **Safety cutoff:** if measured temperature reaches `SAFETY_MAX_TEMP` (130 °C), current is clamped to the minimum and the tick returns phase `OVERTEMP`.

The current phase string (`ramp`, `settle x/y`, `dwell Ns`, `hold-final`, `OVERTEMP`) is recorded in the telemetry CSV.

---

## Machine-Learning Aging Classifier

`plugins/ml_engine.py` implements `RFAgingClassifier`:

- **Model artifacts** loaded from `ml_model_dir`: `<type>_model.joblib`, `scaler.joblib`, and optional `training_metadata.joblib`.
- **Feature extraction** builds a fixed-length vector (≈1,612 elements for 201 points) from complex S11/S21:
  - magnitude in dB, phase in degrees,
  - group delay (ns) from unwrapped phase,
  - magnitude slope (dB/GHz),
  - global statistical summaries (means/stds).
- **Inference** scales the feature vector with the saved `StandardScaler`, predicts a class, and (if the model supports `predict_proba`) returns the probability distribution.
- **Classes:**
  - `0` → Fresh (Baseline)
  - `1` → Lightly Aged (Days 1–3)
  - `2` → Heavily Aged (Days 4+)

Because feature length depends on `points`, the sweep `points` in `config.py` must match what the model was trained on (201).

---

## Blockchain Provenance Ledger

`plugins/blockchain_ledger.py` implements `BlockchainLedger`, an optional tamper-evidence layer. When `enable_blockchain_sync = True`, every acquired sweep is committed to a smart contract on a local Ethereum-compatible chain.

### What it provides

- **Proof of integrity.** Only the file's **SHA-256 hash** (plus lightweight metadata) is written on-chain — never the measurement data itself. Later you can re-hash the `.s2p` file on disk and compare it against the immutable on-chain record; any post-acquisition edit changes the hash and is detectable.
- **Immutable, ordered timeline.** Each record carries a chain `block.timestamp` and an ever-increasing record id, giving an append-only audit trail. This complements the (editable) SHA-256 column already in the telemetry CSV with a record that cannot be silently altered.
- **Attribution.** Every record notes the submitting wallet address.

> **Scope / limitation:** the ledger proves a file is *unchanged since it was committed*; it does **not** prove the sweep itself is physically genuine. On a **local** Hardhat chain the immutability guarantee is only as strong as that node — stronger tamper-resistance requires a shared or persistent chain.

### What is stored on-chain

Each record (via the `DataRegistry.addRecord(...)` contract call) contains:

| Field | Source |
|-------|--------|
| `fileHash` (`bytes32`) | SHA-256 of the `.s2p` file (reuses the hash the loop already computes) |
| `fileName` | Touchstone filename |
| `storageURI` | `file://` absolute path to the on-disk file |
| `fileSizeBytes` | File size |
| `metadataJSON` | JSON: experiment name, ISO time, file type, ML class/label, RTD setpoint/temp |
| `createdAt` | Block timestamp (set by the chain) |
| `submittedBy` | Submitting wallet address (set by the chain) |

The resulting transaction hash is also written to the `blockchain_tx` column of the telemetry CSV, linking each on-disk row to its on-chain record.

### Contract and starter project

The Solidity contract and a minimal Hardhat project (compile + deploy only) live under `blockchain/`. The Python plugin talks to the `DataRegistry` contract via `web3.py` and commits records **live inside the acquisition loop** — there is no separate Node ingestion step.

### Usage

1. **Install web3** in the suite's Python environment:

   ```bash
   pip install web3
   ```

2. **Set up and run the local chain** (from the starter project — needs Node.js + `npm install`):

   ```bash
   cd blockchain
   npm install
   npm run compile
   npm run node        # Terminal 1: local chain on http://127.0.0.1:8545 (keep open)
   ```

3. **Deploy the contract** (Terminal 2). This writes `artifacts_on_chain/contract-address.json`:

   ```bash
   npm run deploy
   ```

4. **Configure the suite** in `config.py`:
   - `enable_blockchain_sync = True`
   - `blockchain_rpc_url` → the chain's JSON-RPC URL (default `http://127.0.0.1:8545`)
   - `blockchain_contract_address_file` → the `contract-address.json` written by the deploy step
   - `blockchain_chain_id` → leave `None` to auto-detect
   - `blockchain_private_key` → leave `None` to use a node-unlocked account (Hardhat default), or set a key to sign transactions explicitly

5. **Run the suite** normally (`python main.py`). On startup it connects to the ledger (logging the contract address and existing record count); each cycle then commits one record and records the tx hash in the telemetry CSV.

### Behavior and fault tolerance

- **Startup:** if `enable_blockchain_sync` is on but the node/contract is unreachable, connection fails during `connect_all_hardware`, so the suite retries connection at boot (same as any other subsystem) rather than starting blind.
- **Per-sweep:** `record_measurement()` is **fail-soft** — if a single commit fails (chain hiccup, timeout), the error is logged, `blockchain_tx` is left blank for that row, and acquisition continues uninterrupted.
- **Dependency isolation:** `web3` is imported lazily, so the suite runs normally without the package installed as long as the feature is disabled.

### Verifying a record

Re-hash a file on disk and compare it to the value stored on-chain:

```bash
sha256sum /path/to/measurement_<timestamp>.s2p
```

Match the printed hash against the `touchstone_sha256` in the telemetry CSV and the `fileHash` in the on-chain record for that `blockchain_tx`.

---

## Output Files

All outputs are written under `base_save_dir/experiment_name/`:

| File | Written by | Contents |
|------|-----------|----------|
| `measurement_<timestamp>.s2p` | `DataStorage` | Touchstone 2-port sweep (one per cycle). |
| `<experiment>_telemetry.csv` | `ExperimentTelemetryLogger` | One synchronized row per cycle (RF filename + checksum, ambient/RTD/DUT telemetry, ML class/label/confidence/probabilities, blockchain tx hash). |
| `<experiment>_events.log` | `setup_system_logger` | Timestamped event/error log. |
| `run_manifest.json` | `ExperimentTelemetryLogger` | Run metadata: creation time, git commit hash, full config snapshot. |

### Telemetry CSV columns

```
iso_time, epoch_time, touchstone_file, touchstone_sha256,
ambient_temp_c, rtd_setpoint_c, rtd_measured_temp_c, rtd_resistance_ohm,
rtd_current_a, rtd_voltage_v, rtd_phase, dut_voltage_v,
dut_current_ma, dut_power_mw,
ml_class, ml_label, ml_confidence,
ml_prob_fresh, ml_prob_light_aged, ml_prob_heavy_aged,
blockchain_tx
```

---

## Fault Tolerance and Recovery

- **Power/communication interruption:** any hardware exception during read/sweep triggers `wait_for_hardware_recovery()`, which logs a `POWER_LOSS_EVENT` telemetry marker, disconnects all handles, then polls every 3 s (re-initializing everything) until the instruments respond. A `POWER_RECOVERY_SUCCESS` marker is written on resume.
- **File-lock / permission resilience:** the event logger, telemetry CSV, and Touchstone writer all detect `PermissionError`/`OSError` and transparently roll over to timestamped `*_fallback_*` files rather than crashing.
- **VNA sweep retries:** individual sweeps retry up to 3 times before escalating to the recovery path.
- **Safe shutdown:** `disconnect_all()` powers down the DUT, closes the PSU output, and releases all serial ports, guarding each step so one failure cannot block the others.

---

## Stopping the Experiment

Press `Ctrl+C`. The controller catches `KeyboardInterrupt`, then in its `finally` block powers down outputs, releases serial ports, and closes the live plot. A "Suite shutdown complete." message confirms clean exit.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|-------------------|
| Stuck at "Hardware not ready on boot" | Check serial port paths in `config.py`; confirm devices are powered and permissions allow access (e.g. user in `dialout` group). |
| Repeated "VNA sweep attempt failed" | USB/cable/power issue on the NanoVNA; the loop will enter recovery until it reconnects. |
| ML `FileNotFoundError` on startup | Verify `ml_model_dir` points to the folder containing `rf_model.joblib` and `scaler.joblib`. |
| Feature-length / shape errors from the model | Ensure `points` matches the training configuration (201). |
| Output written to `*_fallback_*` files | The primary output path was locked/unwritable; check disk permissions and space. |
| Temperature reads implausibly | RTD temperature is derived from PSU V/I; verify CH1 wiring and that current is above the readback floor. |
