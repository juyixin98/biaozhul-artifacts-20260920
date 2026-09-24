// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title HashedTimelock
/// @notice Two-chain HTLC primitive for the local dual-chain demo.
/// @dev State machine: NONEXISTENT -> LOCKED -> (CLAIMED | REFUNDED).
///      CLAIMED and REFUNDED are terminal; a swap can never go back to LOCKED.
///      - claim()  : anyone holding the preimage of hashLock may claim while LOCKED.
///      - refund() : only the original sender, after timelock expiry, while LOCKED.
///      Timestamps are block.timestamp seconds. There is deliberately NO
///      cross-chain messaging: each contract enforces only its local leg.
contract HashedTimelock {
    enum State {
        NONEXISTENT,
        LOCKED,
        CLAIMED,
        REFUNDED
    }

    struct Swap {
        bytes32 hashLock;
        address payable sender;
        address payable receiver;
        uint256 amount;
        uint64 timelock; // unix seconds; refund is allowed at/after this time
        State state;
    }

    mapping(bytes32 => Swap) public swaps;

    event Locked(
        bytes32 indexed swapId,
        bytes32 indexed hashLock,
        address indexed sender,
        address receiver,
        uint256 amount,
        uint64 timelock
    );
    event Claimed(bytes32 indexed swapId, bytes32 preimage, address indexed receiver);
    event Refunded(bytes32 indexed swapId, address indexed sender);

    error SwapExists(bytes32 swapId);
    error UnknownSwap(bytes32 swapId);
    error WrongPreimage(bytes32 provided, bytes32 expected);
    error NotSender(address caller, address sender);
    error TooEarly(uint64 nowTs, uint64 timelock);
    error AlreadySettled(State state);
    error ZeroAmount();
    error ZeroAddress();
    error TimelockInPast(uint64 nowTs, uint64 timelock);

    /// @notice Lock `msg.value` behind a hash lock until `timelock`.
    function lock(
        bytes32 hashLock,
        address payable receiver,
        uint64 timelock
    ) external payable returns (bytes32 swapId) {
        if (msg.value == 0) revert ZeroAmount();
        if (receiver == address(0)) revert ZeroAddress();
        if (timelock <= block.timestamp) revert TimelockInPast(uint64(block.timestamp), timelock);

        swapId = _swapId(hashLock, msg.sender, receiver, msg.value, timelock);
        Swap storage s = swaps[swapId];
        if (s.state != State.NONEXISTENT) revert SwapExists(swapId);

        s.hashLock = hashLock;
        s.sender = payable(msg.sender);
        s.receiver = receiver;
        s.amount = msg.value;
        s.timelock = timelock;
        s.state = State.LOCKED;

        emit Locked(swapId, hashLock, msg.sender, receiver, msg.value, timelock);
    }

    /// @notice Claim with the keccak256 preimage. Works while the swap is LOCKED,
    ///         even past the timelock (whoever reveals first wins).
    function claim(bytes32 swapId, bytes32 preimage) external {
        Swap storage s = swaps[swapId];
        if (s.state == State.NONEXISTENT) revert UnknownSwap(swapId);
        if (s.state != State.LOCKED) revert AlreadySettled(s.state);
        if (keccak256(abi.encodePacked(preimage)) != s.hashLock) {
            revert WrongPreimage(preimage, s.hashLock);
        }

        s.state = State.CLAIMED;
        emit Claimed(swapId, preimage, s.receiver);
        (bool ok, ) = s.receiver.call{value: s.amount}("");
        require(ok, "ETH transfer failed");
    }

    /// @notice Refund after the timelock. Only the original locker may call.
    function refund(bytes32 swapId) external {
        Swap storage s = swaps[swapId];
        if (s.state == State.NONEXISTENT) revert UnknownSwap(swapId);
        if (s.state != State.LOCKED) revert AlreadySettled(s.state);
        if (msg.sender != s.sender) revert NotSender(msg.sender, s.sender);
        if (block.timestamp < s.timelock) revert TooEarly(uint64(block.timestamp), s.timelock);

        s.state = State.REFUNDED;
        emit Refunded(swapId, s.sender);
        (bool ok, ) = s.sender.call{value: s.amount}("");
        require(ok, "ETH transfer failed");
    }

    function getState(bytes32 swapId) external view returns (State) {
        return swaps[swapId].state;
    }

    function _swapId(
        bytes32 hashLock,
        address sender,
        address receiver,
        uint256 amount,
        uint64 timelock
    ) internal pure returns (bytes32) {
        return keccak256(abi.encodePacked(hashLock, sender, receiver, amount, timelock));
    }
}
