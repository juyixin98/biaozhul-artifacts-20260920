// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @title HTLC — 哈希时间锁合约（跨链交换的"单侧"原语）
/// @notice 同一份合约字节码分别部署在 Alpha / Beta 两条本地测试链上。
///         每笔交换在两条链上用同一个 swapId 各建立一个 leg。
///
/// 状态机:  Absent ──lock()──▶ Locked ──claim()──▶ Claimed
///                              │
///                              └──refund()──▶ Refunded
///
/// claim 与 refund 互斥：两者都只在 State.Locked 下成立，且严格按照
/// checks-effects-interactions 先改状态再转币。任何一方成功后，
/// 另一方在任何时间点（即使超过/未到时间锁）都必然 revert。
///
/// 时间约定（详见 README）:
///   Beta  腿时间锁 tB = T0 + βTTL   （领取会在此首次泄露 preimage）
///   Alpha 腿时间锁 tA = T0 + αTTL
///   必须满足 tB < tA；Δ = tA − tB 须覆盖"在 Beta 看到领取 → 在 Alpha 提交领取"
///   的最坏耗时。两条链各自使用本区块 block.timestamp，互不同步。
contract HTLC {
    enum State {
        Absent,   // 不存在（0）
        Locked,   // 已锁定，等待领取或超时（1）
        Claimed,  // 已凭正确原像领取（终态）（2）
        Refunded  // 已超时退款（终态）（3）
    }

    struct Swap {
        address sender;    // 锁定资金者
        address receiver;  // 唯一有权凭原像领取者
        uint256 amount;    // 锁定的原生币 (wei)
        bytes32 hashLock;  // keccak256(abi.encodePacked(preimage))
        uint64 timelock;   // 退款开放的绝对时刻 (unix 秒)
        State state;
    }

    mapping(bytes32 id => Swap) private _swaps;

    event Locked(
        bytes32 indexed id,
        address indexed sender,
        address indexed receiver,
        uint256 amount,
        bytes32 hashLock,
        uint64 timelock
    );
    event Claimed(bytes32 indexed id, bytes32 preimage, address indexed receiver, uint256 amount);
    event Refunded(bytes32 indexed id, address indexed sender, uint256 amount);

    error SwapExists(bytes32 id);
    error SwapNotFound(bytes32 id);
    error ZeroAmount();
    error ZeroReceiver();
    error NotReceiver(address caller);
    error WrongPreimage(bytes32 provided, bytes32 expected);
    error TooEarly(uint64 nowTs, uint64 timelock);
    error AlreadySettled(State state);
    error TransferFailed();

    /// @dev 锁定原生资产。同一 swapId 不可重复锁定。
    function lock(bytes32 id, address receiver, bytes32 hashLock, uint64 timelock)
        external
        payable
    {
        Swap storage s = _swaps[id];
        if (s.state != State.Absent) revert SwapExists(id);
        if (msg.value == 0) revert ZeroAmount();
        if (receiver == address(0)) revert ZeroReceiver();

        s.sender = msg.sender;
        s.receiver = receiver;
        s.amount = msg.value;
        s.hashLock = hashLock;
        s.timelock = timelock;
        s.state = State.Locked;

        emit Locked(id, msg.sender, receiver, msg.value, hashLock, timelock);
    }

    /// @dev 凭 32 字节原像领取，只有 receiver 可调，无时间下限——
    ///      但时间锁到期后 sender 也可能并发退款（见 README 风险边界）。
    function claim(bytes32 id, bytes32 preimage) external {
        Swap storage s = _swaps[id];
        if (s.state == State.Absent) revert SwapNotFound(id);
        if (s.state != State.Locked) revert AlreadySettled(s.state);
        if (msg.sender != s.receiver) revert NotReceiver(msg.sender);

        bytes32 provided = keccak256(abi.encodePacked(preimage));
        if (provided != s.hashLock) revert WrongPreimage(provided, s.hashLock);

        // effects 在 interactions 之前：重入也无法再动这笔 swap。
        address to = s.receiver;
        uint256 amount = s.amount;
        s.state = State.Claimed;

        (bool ok,) = to.call{value: amount}("");
        if (!ok) revert TransferFailed();

        emit Claimed(id, preimage, to, amount);
    }

    /// @dev 超时退款。时间锁未到任何人都不能退；终态后再调必然 revert。
    function refund(bytes32 id) external {
        Swap storage s = _swaps[id];
        if (s.state == State.Absent) revert SwapNotFound(id);
        if (s.state != State.Locked) revert AlreadySettled(s.state);
        if (block.timestamp < s.timelock) {
            revert TooEarly(uint64(block.timestamp), s.timelock);
        }

        address to = s.sender;
        uint256 amount = s.amount;
        s.state = State.Refunded;

        (bool ok,) = to.call{value: amount}("");
        if (!ok) revert TransferFailed();

        emit Refunded(id, to, amount);
    }

    function getSwap(bytes32 id)
        external
        view
        returns (
            address sender,
            address receiver,
            uint256 amount,
            bytes32 hashLock,
            uint64 timelock,
            State state
        )
    {
        Swap storage s = _swaps[id];
        return (s.sender, s.receiver, s.amount, s.hashLock, s.timelock, s.state);
    }

    function stateOf(bytes32 id) external view returns (State) {
        return _swaps[id].state;
    }
}
