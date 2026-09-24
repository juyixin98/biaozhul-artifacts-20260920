"""Application service: reads/writes the MultisigTimelock contract through the
HTTP-facing API. Holds no state beyond a connection and the local key wallet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from eth_account import Account
from hexbytes import HexBytes
from web3 import Web3

from .chain import ChainClient, ChainError
from .config import Settings
from .signing import sign_op, sign_op_digest, sign_set_signers

STATE_NAMES = [
    "None",
    "Proposed",
    "Scheduled",
    "Executed",
    "Failed",
    "Void",
    "Invalidated",
]
TERMINAL_STATES = {"Executed", "Failed", "Void", "Invalidated"}


def decode_hex(value: str | bytes | None) -> bytes:
    if value is None or value == "" or value == "0x":
        return b""
    if isinstance(value, bytes):
        return value
    return HexBytes(value)


@dataclass
class OperationRef:
    target: str
    value: int
    data_hash: str
    nonce: int
    deadline: int
    op_id: str


class TimelockService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.chain = ChainClient(settings.rpc_url, settings.chain_id)
        with open(settings.deployments_path, "r", encoding="utf-8") as fh:
            self.deployment = json.load(fh)
        self.executor_address = self.deployment["executor"]
        self.executor = self.chain.contract(
            "MultisigTimelock", self.executor_address
        )
        self.targets = self.deployment.get("targets", {})
        self.signer_accounts = [
            Account.from_key(key) for key in settings.signer_keys
        ]
        self.operator_account = Account.from_key(settings.operator_key)
        self.error_names = self._build_error_map(self.executor.abi)

    @staticmethod
    def _build_error_map(abi: list[dict]) -> dict[str, str]:
        names: dict[str, str] = {}
        for entry in abi:
            if entry.get("type") != "error":
                continue
            params = ",".join(p["type"] for p in entry["inputs"])
            signature = f"{entry['name']}({params})"
            names[Web3.keccak(text=signature)[:4].hex()] = signature
        return names

    def decode_error(self, reason: str | None) -> str | None:
        """Turn a raw custom-error payload into 'ErrorName(types) <payload>'."""
        if not reason:
            return reason
        if reason.startswith("0x") and len(reason) >= 10:
            name = self.error_names.get(reason[:10])
            if name:
                return f"{name} {reason}"
        return reason

    # ------------------------------------------------------------------ views

    def state(self) -> dict:
        ex = self.executor.functions
        return {
            "rpc_url": self.settings.rpc_url,
            "chain_id": self.chain.chain_id,
            "executor": self.executor_address,
            "signers": ex.signers().call(),
            "threshold": ex.threshold().call(),
            "config_version": ex.configVersion().call(),
            "nonce": ex.nonce().call(),
            "timelock_seconds": ex.timelockSeconds().call(),
            "retry_cooldown_seconds": ex.retryCooldownSeconds().call(),
            "max_failures": ex.maxFailures().call(),
            "now": self.chain.now(),
            "targets": self.targets,
        }

    def op_ref(self, target: str, value: int, data: bytes, nonce: int,
               deadline: int) -> OperationRef:
        data_hash = Web3.keccak(data)
        op_id = self.executor.functions.opId(
            Web3.to_checksum_address(target),
            value,
            data_hash,
            nonce,
            deadline,
        ).call()
        return OperationRef(
            target=Web3.to_checksum_address(target),
            value=value,
            data_hash=data_hash.hex(),
            nonce=nonce,
            deadline=deadline,
            op_id=HexBytes(op_id).hex(),
        )

    def digest_for(self, target: str, value: int, data: bytes, nonce: int,
                   deadline: int) -> dict:
        ref = self.op_ref(target, value, data, nonce, deadline)
        digest = self.executor.functions.digestFor(
            ref.target, value, HexBytes(ref.data_hash), nonce, deadline
        ).call()
        return {
            "op_id": ref.op_id,
            "data_hash": ref.data_hash,
            "digest": HexBytes(digest).hex(),
            "config_version": self.executor.functions.configVersion().call(),
        }

    def get_op(self, op_id: str) -> dict:
        (
            target,
            value,
            data_hash,
            op_nonce,
            deadline,
            config_version,
            state_idx,
            approved_at,
            last_attempt_at,
            failures,
            approval_count,
            ready,
            ready_at,
        ) = self.executor.functions.getOp(HexBytes(op_id)).call()
        if state_idx == 0 and target == "0x" + "00" * 20:
            return {"op_id": op_id, "state": "None", "known": False}
        return {
            "op_id": op_id,
            "known": True,
            "target": target,
            "value": value,
            "data_hash": HexBytes(data_hash).hex(),
            "nonce": op_nonce,
            "deadline": deadline,
            "config_version": config_version,
            "state": STATE_NAMES[state_idx],
            "terminal": STATE_NAMES[state_idx] in TERMINAL_STATES,
            "approved_at": approved_at,
            "last_attempt_at": last_attempt_at,
            "failures": failures,
            "approval_count": approval_count,
            "ready": ready,
            "ready_at": ready_at,
            "now": self.chain.now(),
        }

    # ------------------------------------------------------------------ writes

    def propose(
        self,
        target: str,
        value: int,
        data: bytes,
        deadline: int,
        signer_indices: list[int] | None = None,
        signatures: list[str] | None = None,
    ) -> dict:
        nonce = self.executor.functions.nonce().call()
        config_version = self.executor.functions.configVersion().call()
        ref = self.op_ref(target, value, data, nonce, deadline)
        sigs = self._op_signatures(
            ref, data, nonce, deadline, config_version,
            signer_indices, signatures,
        )
        receipt = self.chain.transact(
            self.executor.functions.propose(
                ref.target, value, data, nonce, deadline,
                [HexBytes(s) for s in sigs],
            ),
            self.settings.operator_key,
        )
        return self._op_result(ref, receipt, "proposed")

    def approve(
        self,
        target: str,
        value: int,
        data_hash: str,
        nonce: int,
        deadline: int,
        signer_indices: list[int] | None = None,
        signatures: list[str] | None = None,
    ) -> dict:
        config_version = self.executor.functions.configVersion().call()
        target = Web3.to_checksum_address(target)
        data_hash_b = HexBytes(data_hash)
        op_id = self.executor.functions.opId(
            target, value, data_hash_b, nonce, deadline
        ).call()
        sigs = self._hash_signatures(
            target, value, data_hash_b, nonce, deadline, config_version,
            signer_indices, signatures,
        )
        receipt = self.chain.transact(
            self.executor.functions.approve(
                target, value, data_hash_b, nonce, deadline,
                [HexBytes(s) for s in sigs],
            ),
            self.settings.operator_key,
        )
        return self._op_result(
            OperationRef(target, value, data_hash, nonce, deadline,
                         HexBytes(op_id).hex()),
            receipt, "approved",
        )

    def execute(self, target: str, value: int, data: bytes, nonce: int,
                deadline: int) -> dict:
        target = Web3.to_checksum_address(target)
        ref = self.op_ref(target, value, data, nonce, deadline)
        receipt = self.chain.transact(
            self.executor.functions.execute(target, value, data, nonce, deadline),
            self.settings.operator_key,
        )
        result = self._op_result(ref, receipt, "execute_submitted")
        # The target call may have failed without reverting the outer tx: the
        # op stays Scheduled with an incremented failure counter.
        op = self.get_op(ref.op_id)
        result["op"] = op
        result["action"] = (
            "executed" if op["state"] == "Executed"
            else "failed_retryable" if op["state"] == "Scheduled"
            else "retries_exhausted"
        )
        return result

    def change_signers(
        self,
        new_signers: list[str],
        new_threshold: int,
        deadline: int,
        signer_indices: list[int] | None = None,
        signatures: list[str] | None = None,
    ) -> dict:
        nonce = self.executor.functions.nonce().call()
        config_version = self.executor.functions.configVersion().call()
        new_signers = [Web3.to_checksum_address(s) for s in new_signers]
        sigs = self._set_signers_signatures(
            new_signers, new_threshold, nonce, deadline, config_version,
            signer_indices, signatures,
        )
        receipt = self.chain.transact(
            self.executor.functions.setSigners(
                new_signers, new_threshold, nonce, deadline,
                [HexBytes(s) for s in sigs],
            ),
            self.settings.operator_key,
        )
        return {
            "status": "ok",
            "action": "signers_changed",
            "tx_hash": receipt["transactionHash"].hex(),
            "block_number": receipt["blockNumber"],
            "signers": new_signers,
            "threshold": new_threshold,
            "config_version": self.executor.functions.configVersion().call(),
        }

    def void_expired(self, op_id: str) -> dict:
        receipt = self.chain.transact(
            self.executor.functions.voidExpired(HexBytes(op_id)),
            self.settings.operator_key,
        )
        return self._op_result(
            OperationRef("", 0, "", 0, 0, op_id), receipt, "voided"
        )

    def flip_flaky(self, failing: bool) -> dict:
        """Dev helper: toggle FlakyTarget between failing and succeeding."""
        address = self.targets.get("flaky")
        if not address:
            raise ChainError("no FlakyTarget in deployment")
        flaky = self.chain.contract("FlakyTarget", address)
        receipt = self.chain.transact(
            flaky.functions.setFailing(failing),
            self.settings.operator_key,
        )
        return {
            "status": "ok",
            "failing": failing,
            "tx_hash": receipt["transactionHash"].hex(),
        }

    # -------------------------------------------------------------- internals

    def _op_signatures(self, ref: OperationRef, data: bytes, nonce: int,
                       deadline: int, config_version: int,
                       signer_indices: list[int] | None,
                       signatures: list[str] | None) -> list[str]:
        return self._hash_signatures(
            ref.target, ref.value, HexBytes(ref.data_hash), nonce, deadline,
            config_version, signer_indices, signatures,
            # data only needed when signing locally instead of passed hashes:
            data_for_signing=data,
        )

    def _hash_signatures(self, target: str, value: int, data_hash: HexBytes,
                         nonce: int, deadline: int, config_version: int,
                         signer_indices: list[int] | None,
                         signatures: list[str] | None,
                         data_for_signing: bytes | None = None) -> list[str]:
        if signatures is not None:
            return [self._validate_signature(s) for s in signatures]

        # Reconstruct the preimage for local signing. Approve-only callers that
        # have no bytes (only the hash) must pass explicit signatures.
        if signer_indices is None:
            indices = list(range(len(self.signer_accounts)))
        else:
            indices = signer_indices
        self._validate_indices(indices)

        if data_for_signing is not None:
            make_sig = lambda key: sign_op(  # noqa: E731
                key, self.chain.chain_id, self.executor_address,
                target, value, data_for_signing, nonce, deadline,
                config_version,
            )
        else:
            # approve() path: only the calldata hash is available.
            make_sig = lambda key: sign_op_digest(  # noqa: E731
                key, self.chain.chain_id, self.executor_address,
                target, value, bytes(data_hash), nonce, deadline,
                config_version,
            )
        return [make_sig(self.settings.signer_keys[i]) for i in indices]

    def _set_signers_signatures(self, new_signers, new_threshold, nonce,
                                deadline, config_version, signer_indices,
                                signatures) -> list[str]:
        if signatures is not None:
            return [self._validate_signature(s) for s in signatures]
        if signer_indices is None:
            indices = list(range(len(self.signer_accounts)))
        else:
            indices = signer_indices
        self._validate_indices(indices)
        return [
            sign_set_signers(
                self.settings.signer_keys[i],
                self.chain.chain_id,
                self.executor_address,
                new_signers, new_threshold, nonce, deadline, config_version,
            )
            for i in indices
        ]

    def _validate_indices(self, indices: list[int]) -> None:
        for i in indices:
            if not 0 <= i < len(self.signer_accounts):
                raise ChainError(
                    f"signer index {i} out of range "
                    f"(0..{len(self.signer_accounts) - 1})"
                )

    @staticmethod
    def _validate_signature(sig: str) -> str:
        raw = HexBytes(sig)
        if len(raw) != 65:
            raise ChainError(f"signature must be 65 bytes, got {len(raw)}")
        return sig if sig.startswith("0x") else "0x" + sig

    def _op_result(self, ref: OperationRef, receipt: dict, action: str) -> dict:
        return {
            "status": "ok",
            "action": action,
            "op_id": ref.op_id,
            "tx_hash": receipt["transactionHash"].hex(),
            "block_number": receipt["blockNumber"],
            "op": self.get_op(ref.op_id),
        }
