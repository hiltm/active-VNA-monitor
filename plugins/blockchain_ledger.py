# plugins/blockchain_ledger.py
"""
Blockchain provenance ledger for the active-VNA-monitor suite.

Commits a tamper-evident record of every acquired sweep to a local
Ethereum-compatible chain (Hardhat) via the `DataRegistry` smart contract.

Only the SHA-256 hash + lightweight metadata is written on-chain; the actual
Touchstone `.s2p` file stays on disk. This lets you later re-hash a file and
compare it against the immutable on-chain record to prove it was not altered
after acquisition.

The contract ABI mirrors blockchain/contracts/DataRegistry.sol. The plugin is
fail-soft: any connection or transaction error is logged and swallowed so the
acquisition loop keeps running.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("VNASuite")

# Minimal ABI for DataRegistry.addRecord / getRecordCount. Matches the
# Solidity contract in the tmp starter project.
DATA_REGISTRY_ABI = [
    {
        "inputs": [
            {"internalType": "bytes32", "name": "fileHash", "type": "bytes32"},
            {"internalType": "string", "name": "fileName", "type": "string"},
            {"internalType": "string", "name": "storageURI", "type": "string"},
            {"internalType": "uint256", "name": "fileSizeBytes", "type": "uint256"},
            {"internalType": "string", "name": "metadataJSON", "type": "string"},
        ],
        "name": "addRecord",
        "outputs": [{"internalType": "uint256", "name": "recordId", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getRecordCount",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class BlockchainLedger:
    """
    Thin web3.py client that appends per-sweep provenance records to a deployed
    DataRegistry contract on a local JSON-RPC chain.
    """

    def __init__(
        self,
        rpc_url: str,
        contract_address_file: str,
        chain_id: Optional[int] = None,
        private_key: Optional[str] = None,
        tx_timeout: float = 30.0,
    ):
        self.rpc_url = rpc_url
        self.contract_address_file = Path(contract_address_file)
        self.chain_id = chain_id
        self.private_key = private_key
        self.tx_timeout = tx_timeout

        self.w3 = None
        self.contract = None
        self.account = None  # sender address string

    def _resolve_contract_address(self) -> str:
        """Reads the deployed address from the Hardhat deploy artifact."""
        if not self.contract_address_file.exists():
            raise FileNotFoundError(
                f"Contract address file not found: {self.contract_address_file}. "
                f"Deploy the contract first (npm run deploy)."
            )
        data = json.loads(self.contract_address_file.read_text(encoding="utf-8"))
        address = data.get("DataRegistry")
        if not address:
            raise ValueError(
                f"'DataRegistry' key missing in {self.contract_address_file}"
            )
        return address

    def connect(self):
        """
        Establishes the web3 connection, loads the contract, and selects the
        sending account. Raises on failure so connect_all_hardware() can treat
        the ledger like any other subsystem.
        """
        # Imported lazily so the suite runs without web3 installed when the
        # blockchain feature is disabled.
        from web3 import Web3

        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        if not self.w3.is_connected():
            raise ConnectionError(f"Cannot reach JSON-RPC node at {self.rpc_url}")

        raw_address = self._resolve_contract_address()
        address = Web3.to_checksum_address(raw_address)
        self.contract = self.w3.eth.contract(address=address, abi=DATA_REGISTRY_ABI)

        # Account selection: explicit private key, else first node account
        # (Hardhat auto-unlocks its dev accounts).
        if self.private_key:
            acct = self.w3.eth.account.from_key(self.private_key)
            self.account = acct.address
        else:
            accounts = self.w3.eth.accounts
            if not accounts:
                raise RuntimeError("No unlocked accounts available on the node.")
            self.account = accounts[0]
            self.w3.eth.default_account = self.account

        if self.chain_id is None:
            self.chain_id = self.w3.eth.chain_id

        count = self.contract.functions.getRecordCount().call()
        logger.info(
            f"Blockchain ledger connected: {self.rpc_url} contract={address} "
            f"sender={self.account} existing_records={count}"
        )

    def _send_signed(self, fn) -> str:
        """Builds, signs, and sends a transaction from a local private key."""
        nonce = self.w3.eth.get_transaction_count(self.account)
        tx = fn.build_transaction(
            {
                "chainId": self.chain_id,
                "from": self.account,
                "nonce": nonce,
                "gas": 500000,
            }
        )
        signed = self.w3.eth.account.sign_transaction(tx, private_key=self.private_key)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = self.w3.eth.send_raw_transaction(raw)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.tx_timeout)
        return receipt.transactionHash.hex()

    def _send_unlocked(self, fn) -> str:
        """Sends a transaction from a node-unlocked account (Hardhat default)."""
        tx_hash = fn.transact({"from": self.account})
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.tx_timeout)
        return receipt.transactionHash.hex()

    def record_measurement(
        self,
        s2p_path: Path,
        sha256_hex: str,
        metadata: Dict[str, Any],
    ) -> Optional[str]:
        """
        Commits one sweep's provenance record on-chain.

        Args:
            s2p_path: Path to the saved Touchstone file (for name/size/URI).
            sha256_hex: Hex SHA-256 already computed by the acquisition loop.
            metadata: Extra fields serialized into the on-chain metadataJSON.

        Returns:
            The transaction hash (hex str) on success, or None on any failure
            (fail-soft — the acquisition loop must not be interrupted).
        """
        if self.contract is None or self.w3 is None:
            logger.warning("Blockchain ledger not connected; skipping record.")
            return None

        if not sha256_hex or sha256_hex in ("HASH_ERROR", "N/A"):
            logger.warning(f"Skipping on-chain record: invalid hash {sha256_hex!r}")
            return None

        try:
            s2p_path = Path(s2p_path)
            file_name = s2p_path.name
            storage_uri = f"file://{s2p_path.resolve()}"
            try:
                file_size = s2p_path.stat().st_size
            except OSError:
                file_size = 0
            metadata_json = json.dumps(metadata, default=str)

            # Normalize hash to 0x-prefixed 32-byte value.
            hash_arg = sha256_hex if sha256_hex.startswith("0x") else "0x" + sha256_hex

            fn = self.contract.functions.addRecord(
                hash_arg,
                file_name,
                storage_uri,
                file_size,
                metadata_json,
            )

            if self.private_key:
                tx_hex = self._send_signed(fn)
            else:
                tx_hex = self._send_unlocked(fn)

            if not tx_hex.startswith("0x"):
                tx_hex = "0x" + tx_hex
            logger.info(f"On-chain record committed for {file_name}: tx={tx_hex}")
            return tx_hex

        except Exception as e:
            logger.warning(f"Blockchain record failed (continuing): {e}")
            return None

    def close(self):
        """No persistent socket to release; reset handles for reconnection."""
        self.contract = None
        self.w3 = None
        self.account = None
