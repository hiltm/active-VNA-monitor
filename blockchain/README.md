# DataRegistry — Local Provenance Chain

Minimal Hardhat project for the `active-VNA-monitor` suite. Its only job is to
**compile** the `DataRegistry` contract and **deploy** it to a local chain so the
Python `BlockchainLedger` plugin can commit per-sweep provenance records to it.

Record ingestion is handled **live inside the acquisition loop** by
`plugins/blockchain_ledger.py` (via `web3.py`) — there is no separate Node
ingestion step here.

## Contents

```
blockchain/
├── contracts/DataRegistry.sol   # the on-chain provenance contract
├── scripts/deploy.js            # deploys it, writes artifacts_on_chain/contract-address.json
├── hardhat.config.js
└── package.json
```

## Prerequisites

- Node.js 20 or 22 LTS + npm.

## Setup

```bash
cd blockchain          # this folder, inside the active-VNA-monitor repo
npm install
npm run compile
```

## Run the local chain (Terminal 1)

```bash
npm run node           # local JSON-RPC chain on http://127.0.0.1:8545 (keep open)
```

## Deploy the contract (Terminal 2)

```bash
npm run deploy
```

This writes the deployed address to:

```text
artifacts_on_chain/contract-address.json
```

Point `blockchain_contract_address_file` in the suite's `config.py` at that file,
set `enable_blockchain_sync = True`, and run `python main.py`. Each sweep is then
committed on-chain and its transaction hash is recorded in the telemetry CSV.

See the main project [README](../README.md#blockchain-provenance-ledger) for the
full ledger workflow and verification steps.

## What is stored on-chain

Per record (via `DataRegistry.addRecord(...)`):

- SHA-256 hash of the `.s2p` file
- filename
- storage URI/path
- file size
- metadata JSON
- blockchain timestamp (set by the chain)
- submitting wallet address (set by the chain)

Only the hash + metadata are stored — never the measurement file itself.
