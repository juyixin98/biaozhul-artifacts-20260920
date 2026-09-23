// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @notice 故意写坏的 HTLC：无重入互斥锁，且「先外部转账、后更新状态」（违反 CEI）。
///         仅用于在测试中真实演示重入攻击如何得手，以此对照 src/HashTimeLock.sol 的防护。
///         切勿用于任何真实用途。
contract UnsafeHTLC {
    enum State {
        ABSENT,
        LOCKED,
        CLAIMED,
        REFUNDED
    }

    struct Lock {
        address sender;
        address receiver;
        address token;
        uint256 amount;
        bytes32 hashlock;
        uint256 timelock;
        State state;
    }

    mapping(bytes32 => Lock) public locks;
    uint256 private _nonce;

    event Locked(bytes32 indexed id);
    event Claimed(bytes32 indexed id, bytes32 preimage);

    function lock(address receiver, address token, uint256 amount, bytes32 hashlock, uint256 timelock)
        external
        returns (bytes32 id)
    {
        require(receiver != address(0) && amount > 0 && timelock > block.timestamp, "bad lock");
        (bool ok,) = token.call(
            abi.encodeWithSignature("transferFrom(address,address,uint256)", msg.sender, address(this), amount)
        );
        require(ok, "pull failed");
        id = keccak256(abi.encode(msg.sender, receiver, token, amount, hashlock, timelock, _nonce++));
        locks[id] = Lock(msg.sender, receiver, token, amount, hashlock, timelock, State.LOCKED);
    }

    /// @dev BUG：转账（带攻击者回调）发生在状态更新之前，且没有互斥锁。
    function claim(bytes32 id, bytes32 preimage) external {
        Lock storage l = locks[id];
        require(l.state == State.LOCKED, "not locked");
        require(block.timestamp < l.timelock, "too late");
        require(keccak256(abi.encodePacked(preimage)) == l.hashlock, "wrong preimage");

        (bool ok,) = l.token.call(abi.encodeWithSignature("transfer(address,uint256)", l.receiver, l.amount));
        require(ok, "transfer failed");

        l.state = State.CLAIMED;
        emit Claimed(id, preimage);
    }

    function stateOf(bytes32 id) external view returns (State) {
        return locks[id].state;
    }
}
