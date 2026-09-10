"""Arbitrum Sepolia — cryptographic Proof of Thought.

Anchors the Executive's reasoning hash in a 0-value tx `data` field.
Never used for value transfer. Testnet only (chain ID 421614).
"""

from __future__ import annotations

from typing import Any

from web3 import Web3

from core.config import Settings
from core.retry import call_with_backoff
from core.schemas import AttestationResult

EXPLORER_TX = "https://sepolia.arbiscan.io/tx/{tx_hash}"
PROOF_PREFIX = b"CHRONOS-NEXUS/v1:"


class ArbitrumSepolia:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.expected_chain_id = int(settings.arbitrum_sepolia_chain_id or 421614)
        self.w3 = Web3(Web3.HTTPProvider(settings.arbitrum_sepolia_rpc, request_kwargs={"timeout": 20}))
        self._inject_poa()
        self.account = None
        pk = settings.arbitrum_private_key
        if pk:
            self.account = self.w3.eth.account.from_key(pk)

    def _inject_poa(self) -> None:
        try:
            from web3.middleware import ExtraDataToPOAMiddleware

            self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        except Exception:
            try:
                from web3.middleware import geth_poa_middleware

                self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            except Exception:
                pass

    def _rpc(self, fn, *, label: str = "arb"):
        return call_with_backoff(fn, attempts=3, label=label)

    def ping(self) -> dict[str, Any]:
        def _connected() -> bool:
            if not self.w3.is_connected():
                raise TimeoutError(f"Cannot reach Arbitrum RPC {self.settings.arbitrum_sepolia_rpc}")
            return True

        try:
            self._rpc(_connected, label="arb.ping")
            chain_id = int(self._rpc(lambda: self.w3.eth.chain_id, label="arb.chain_id"))
        except Exception as exc:
            raise RuntimeError(f"Cannot reach Arbitrum RPC {self.settings.arbitrum_sepolia_rpc}: {exc}") from exc
        if chain_id != self.expected_chain_id:
            raise RuntimeError(
                f"Wrong chain: got {chain_id}, expected Arbitrum Sepolia {self.expected_chain_id}"
            )
        head = int(self._rpc(lambda: self.w3.eth.block_number, label="arb.block"))
        return {
            "connected": True,
            "chain_id": chain_id,
            "block": head,
            "rpc": self.settings.arbitrum_sepolia_rpc,
            "address": self.account.address if self.account else None,
        }

    def read_balance(self) -> dict[str, Any]:
        if self.account is None:
            return {"ok": False, "error": "ARBITRUM_PRIVATE_KEY missing", "eth": 0.0}
        try:
            wei = self._rpc(lambda: self.w3.eth.get_balance(self.account.address), label="arb.balance")
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:220], "eth": 0.0, "address": self.account.address}
        return {
            "ok": True,
            "address": self.account.address,
            "wei": int(wei),
            "eth": float(self.w3.from_wei(wei, "ether")),
        }

    def log_proof_of_thought(self, trade_data_hash: str) -> AttestationResult:
        """Submit a 0-value self-tx whose input data is the decision hash."""
        digest = _normalize_hash(trade_data_hash)
        if self.account is None:
            return AttestationResult(
                ok=False,
                skipped=True,
                reason="no_private_key",
                chain_id=self.expected_chain_id,
            )

        try:
            ping = self.ping()
        except Exception as exc:
            return AttestationResult(
                ok=False,
                skipped=True,
                reason=f"rpc_error:{exc}"[:220],
                chain_id=self.expected_chain_id,
            )

        payload = PROOF_PREFIX + digest.encode("ascii")
        from_addr = self.account.address
        try:
            nonce = self._rpc(lambda: self.w3.eth.get_transaction_count(from_addr), label="arb.nonce")
        except Exception as exc:
            return AttestationResult(
                ok=False,
                skipped=True,
                from_address=from_addr,
                reason=f"rpc_error:{exc}"[:220],
                chain_id=self.expected_chain_id,
            )
        tx: dict[str, Any] = {
            "chainId": self.expected_chain_id,
            "from": from_addr,
            "to": from_addr,
            "value": 0,
            "nonce": nonce,
            "data": payload,
        }

        try:
            latest = self._rpc(lambda: self.w3.eth.get_block("latest"), label="arb.head")
            base_fee = int(latest.get("baseFeePerGas") or self.w3.to_wei(0.1, "gwei"))
        except Exception:
            base_fee = int(self.w3.to_wei(0.1, "gwei"))
        priority = int(self.w3.to_wei(0.01, "gwei"))
        max_fee = base_fee * 2 + priority
        tx["maxPriorityFeePerGas"] = priority
        tx["maxFeePerGas"] = max_fee

        try:
            gas = int(self._rpc(lambda: self.w3.eth.estimate_gas(tx), label="arb.estimate_gas"))
        except Exception:
            gas = 21_000 + 16 * (len(payload) + 4)
        tx["gas"] = int(gas * 1.2) + 1_000

        try:
            balance = int(self._rpc(lambda: self.w3.eth.get_balance(from_addr), label="arb.balance.attest"))
        except Exception:
            balance = 0
        needed = int(tx["gas"]) * int(max_fee)
        if balance < needed:
            return AttestationResult(
                ok=False,
                skipped=True,
                from_address=from_addr,
                chain_id=ping["chain_id"],
                reason="insufficient_gas",
            )

        try:
            signed = self.account.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
            # Do not blindly retry a send: a timeout may mean the tx already landed.
            tx_hash = call_with_backoff(
                lambda: self.w3.eth.send_raw_transaction(raw),
                attempts=2,
                label="arb.send_raw",
            )
            hex_hash = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
            if not hex_hash.startswith("0x"):
                hex_hash = "0x" + hex_hash
            return AttestationResult(
                ok=True,
                skipped=False,
                tx_hash=hex_hash,
                explorer_url=EXPLORER_TX.format(tx_hash=hex_hash),
                from_address=from_addr,
                chain_id=ping["chain_id"],
                reason="anchored",
            )
        except Exception as exc:
            msg = str(exc)
            lowered = msg.lower()
            if "insufficient funds" in lowered or "intrinsic gas" in lowered:
                reason = "insufficient_gas"
            else:
                reason = msg[:220]
            return AttestationResult(
                ok=False,
                skipped=True,
                from_address=from_addr,
                chain_id=self.expected_chain_id,
                reason=reason,
            )


def _normalize_hash(value: str) -> str:
    digest = value.strip().lower().replace("0x", "")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("trade_data_hash must be a 32-byte hex sha256")
    return digest
