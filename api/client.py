"""web3.py wrapper around the deployed SyntheticToken + LinearTokenVesting."""
from __future__ import annotations

from dataclasses import dataclass

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from . import config


class ContractReverted(Exception):
    """Raised when a state-changing call simulates or mines as a revert."""


@dataclass
class ScheduleView:
    id: int
    beneficiary: str
    total_amount: int
    released_amount: int
    start: int
    cliff: int
    end: int
    revocable: bool
    revoked: bool
    revoked_at: int
    refunded_amount: int
    vested_now: int
    releasable_now: int


class VestingClient:
    def __init__(
        self,
        rpc_url: str | None = None,
        token_address: str | None = None,
        vesting_address: str | None = None,
    ) -> None:
        self.w3 = Web3(Web3.HTTPProvider(rpc_url or config.RPC_URL))
        # Anvil is treated like a PoA/zero-gas-price chain by web3.py v7.
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        addresses = config.load_deployed_addresses()
        token_address = Web3.to_checksum_address(
            token_address or addresses["token"]
        )
        vesting_address = Web3.to_checksum_address(
            vesting_address or addresses["vesting"]
        )

        self.token = self.w3.eth.contract(
            address=token_address, abi=config.load_abi("SyntheticToken")
        )
        self.vesting = self.w3.eth.contract(
            address=vesting_address, abi=config.load_abi("LinearTokenVesting")
        )

    # ------------------------------------------------------------------
    # Chain helpers
    # ------------------------------------------------------------------

    def _send(self, account, tx: dict) -> dict:
        """Sign with a local private key and wait for the receipt (local Anvil)."""
        if isinstance(account, str) and account.startswith("0x") and len(account) == 66:
            acct = self.w3.eth.account.from_key(account)
        else:
            acct = account
        tx["from"] = acct.address
        tx.setdefault("chainId", self.w3.eth.chain_id)
        tx.setdefault("gas", 3_000_000)
        tx["nonce"] = self.w3.eth.get_transaction_count(acct.address)
        # web3.py v7 fills EIP-1559 fees itself; setting gasPrice is rejected.
        tx.setdefault("maxFeePerGas", self.w3.eth.gas_price * 2)
        tx.setdefault("maxPriorityFeePerGas", self.w3.to_wei(1, "gwei"))

        # Simulate first so a contract revert surfaces as an exception carrying
        # the Solidity revert reason (send_raw_transaction would otherwise only
        # report status=0 in the receipt).
        try:
            self.w3.eth.call(dict(tx))
        except Exception as exc:
            raise ContractReverted(str(exc)) from exc

        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = self.w3.eth.send_raw_transaction(raw)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        if receipt["status"] != 1:
            raise ContractReverted(f"transaction reverted on-chain: {tx_hash.hex()}")
        return {
            "tx_hash": tx_hash.hex(),
            "block_number": receipt["blockNumber"],
            "status": receipt["status"],
            "gas_used": receipt["gasUsed"],
        }

    # ------------------------------------------------------------------
    # Read views
    # ------------------------------------------------------------------

    def chain_status(self) -> dict:
        block = self.w3.eth.get_block("latest")
        return {
            "rpc": self.w3.provider.endpoint_uri,
            "chain_id": self.w3.eth.chain_id,
            "block_number": block["number"],
            "timestamp": block["timestamp"],
            "token": self.token.address,
            "vesting": self.vesting.address,
            "token_name": self.token.functions.name().call(),
            "token_symbol": self.token.functions.symbol().call(),
            "token_total_supply": self.token.functions.totalSupply().call(),
            "escrow_balance": self.token.functions.balanceOf(
                self.vesting.address
            ).call(),
        }

    def token_balance(self, address: str) -> int:
        return self.token.functions.balanceOf(
            Web3.to_checksum_address(address)
        ).call()

    def get_schedule(self, schedule_id: int) -> ScheduleView:
        s = self.vesting.functions.getSchedule(schedule_id).call()
        now = self.w3.eth.get_block("latest")["timestamp"]
        vested = self.vesting.functions.vestedAmount(schedule_id, now).call()
        releasable = self.vesting.functions.releasableAmount(schedule_id).call()
        # getSchedule returns the struct field order from the Solidity definition.
        return ScheduleView(
            id=schedule_id,
            beneficiary=s[0],
            total_amount=s[1],
            released_amount=s[2],
            start=s[3],
            cliff=s[4],
            end=s[5],
            revocable=s[6],
            revoked=s[7],
            revoked_at=s[8],
            refunded_amount=s[9],
            vested_now=vested,
            releasable_now=releasable,
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def create_schedule(
        self,
        owner_key: str,
        beneficiary: str,
        amount: int,
        start_timestamp: int,
        cliff_duration: int,
        vesting_duration: int,
        revocable: bool,
        approve: bool = True,
    ) -> dict:
        owner = self.w3.eth.account.from_key(owner_key)
        result: dict = {}
        if approve:
            result["approve"] = self._send(
                owner,
                self.token.functions.approve(
                    self.vesting.address, amount
                ).build_transaction({"from": owner.address, "gas": 200_000}),
            )
        result["create"] = self._send(
            owner,
            self.vesting.functions.createSchedule(
                Web3.to_checksum_address(beneficiary),
                amount,
                start_timestamp,
                cliff_duration,
                vesting_duration,
                revocable,
            ).build_transaction({"from": owner.address, "gas": 500_000}),
        )
        result["schedule_id"] = self.vesting.functions.nextScheduleId().call() - 1
        return result

    def release(self, caller_key: str, schedule_id: int) -> dict:
        return self._send(
            caller_key,
            self.vesting.functions.release(schedule_id).build_transaction(
                {"gas": 300_000}
            ),
        )

    def revoke(self, owner_key: str, schedule_id: int) -> dict:
        return self._send(
            owner_key,
            self.vesting.functions.revoke(schedule_id).build_transaction(
                {"gas": 300_000}
            ),
        )

    def evm_set_time(self, timestamp: int) -> int:
        """Anvil test helper: set the chain clock to ``timestamp``.

        Uses ``evm_setTime`` (absolute clock adjustment, not the one-shot
        ``evm_setNextBlockTimestamp``): every subsequently mined block —
        including the action transaction right after an ``eth_call``
        simulation — carries exactly this timestamp until the clock moves
        again. Returns the number of seconds adjusted.
        """
        result = self.w3.provider.make_request("evm_setTime", [timestamp])
        return int(result.get("result", 0), 16) if isinstance(
            result.get("result"), str
        ) else int(result.get("result", 0) or 0)

    def evm_mine(self) -> bool:
        return self.w3.provider.make_request("evm_mine", [])

    def pin_next_timestamp(self, timestamp: int) -> None:
        """Pin the chain clock to ``timestamp`` for upcoming operations.

        ``evm_setTime`` only takes effect when a block is produced, while the
        pre-send ``eth_call`` simulation evaluates against the *latest* block.
        Mine one block at ``timestamp`` so that both the simulation and the
        following action transaction see the exact same timestamp.
        """
        self.evm_set_time(timestamp)
        self.evm_mine()

    def sleep_chain(self, seconds: int) -> int:
        """Advance the chain clock (for view checkpoints) and mine one block."""
        now = self.w3.eth.get_block("latest")["timestamp"]
        target = now + seconds
        self.evm_set_time(target)
        self.evm_mine()
        return target
