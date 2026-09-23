// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "./IERC20.sol";

/// @title HashTimeLock —— 原子兑换中的单侧哈希时间锁（ERC-20 版本）
/// @notice 一笔锁定（lock）严格绑定四要素：接收者 receiver、哈希摘要 hashLock、金额 amount、
///         截止时间 timelock；领取（claim）必须出示 sha256 原像且在截止前；
///         到期后只允许发送者退款（refund）。
/// @dev    状态机：NONE（不存在）→ LOCKED → CLAIMED / REFUNDED。
///         每个 lockId 只能离开 LOCKED 一次，终态不可逆。
///         哈希采用 SHA-256（与比特币 HTLC、跨链工具链一致）；原像为任意字节串。
contract HashTimeLock {
    enum State {
        NONE, // 0：锁定不存在
        LOCKED, // 1：已锁定，等待领取或到期退款
        CLAIMED, // 2：终态——接收者凭原像领取
        REFUNDED // 3：终态——到期后发送者收回
    }

    struct Lock {
        address sender; // 锁定资金者（到期可退款的人）
        address receiver; // 唯一有权凭原像领取的人
        address token; // 被锁定的 ERC-20 地址
        uint256 amount; // 锁定金额
        bytes32 hashLock; // sha256(secret)
        uint64 timelock; // 截止时间（Unix 秒，绝对时间）
        State state;
    }

    /// @dev 简单的重入锁；不使用 openzeppelin，保持零外部依赖。
    uint256 private constant _NOT_ENTERED = 1;
    uint256 private constant _ENTERED = 2;
    uint256 private _reentrancyStatus = _NOT_ENTERED;

    uint256 public lockCount;
    mapping(uint256 => Lock) private _locks;

    /// @notice 资金锁定成功。lockId 从 1 开始单调递增。
    event Locked(
        uint256 indexed lockId,
        address indexed sender,
        address indexed receiver,
        address token,
        uint256 amount,
        bytes32 hashLock,
        uint64 timelock
    );

    /// @notice 资金被接收者凭原像领取（终态）。
    /// @dev    原像随事件公开上链：这正是原子兑换的关键——任何人（含对端链的发送者）
    ///         都能从收据日志中读到它。
    event Claimed(uint256 indexed lockId, bytes secret);

    /// @notice 到期后发送者收回资金（终态）。
    event Refunded(uint256 indexed lockId);

    error ZeroAddress();
    error ZeroAmount();
    error TimelockInPast();
    error UnknownLock();
    error NotLocked();
    error NotReceiver();
    error NotSender();
    error StillLocked();
    error LockExpired();
    error WrongSecret();
    error EmptySecret();
    error ReentrantCall();

    modifier nonReentrant() {
        if (_reentrancyStatus == _ENTERED) revert ReentrantCall();
        _reentrancyStatus = _ENTERED;
        _;
        _reentrancyStatus = _NOT_ENTERED;
    }

    // ---------------------------------------------------------------------
    // 锁定
    // ---------------------------------------------------------------------

    /// @notice 存入 ERC-20 并创建一笔哈希时间锁。
    /// @param  receiver  唯一领取人
    /// @param  token     被锁代币合约
    /// @param  amount    金额（最小单位），必须 > 0
    /// @param  hashLock  sha256(secret)
    /// @param  timelock  绝对截止时间（Unix 秒），必须晚于当前区块时间
    /// @return lockId    新锁的 id（从 1 开始）
    function lock(address receiver, address token, uint256 amount, bytes32 hashLock, uint64 timelock)
        external
        nonReentrant
        returns (uint256 lockId)
    {
        if (receiver == address(0) || token == address(0)) revert ZeroAddress();
        if (amount == 0) revert ZeroAmount();
        if (timelock <= block.timestamp) revert TimelockInPast();

        lockId = ++lockCount;
        _locks[lockId] = Lock({
            sender: msg.sender,
            receiver: receiver,
            token: token,
            amount: amount,
            hashLock: hashLock,
            timelock: timelock,
            state: State.LOCKED
        });

        emit Locked(lockId, msg.sender, receiver, token, amount, hashLock, timelock);

        // 状态先落库（Checks-Effects-Interactions），再做外部转账。
        if (!IERC20(token).transferFrom(msg.sender, address(this), amount)) {
            revert("HashTimeLock: token transfer failed");
        }
    }

    // ---------------------------------------------------------------------
    // 领取（唯一离开 LOCKED 的路径之一）
    // ---------------------------------------------------------------------

    /// @notice 接收者在截止时间前出示原像领取资金。
    /// @dev    严格检查顺序：存在 → 仍锁定 → 调用者是接收者 → 未到期 → 原像正确。
    ///         先置终态再转账，防止恶意代币回调重入。
    function claim(uint256 lockId, bytes calldata secret) external nonReentrant {
        Lock storage l = _locks[lockId];
        if (l.state == State.NONE) revert UnknownLock();
        if (l.state != State.LOCKED) revert NotLocked();
        if (msg.sender != l.receiver) revert NotReceiver();
        // 时间是“恰好到期即失效”：block.timestamp >= timelock 时领取必然失败。
        if (block.timestamp >= l.timelock) revert LockExpired();
        if (secret.length == 0) revert EmptySecret();
        if (sha256(secret) != l.hashLock) revert WrongSecret();

        l.state = State.CLAIMED;
        emit Claimed(lockId, secret);

        if (!IERC20(l.token).transfer(l.receiver, l.amount)) {
            revert("HashTimeLock: token transfer failed");
        }
    }

    // ---------------------------------------------------------------------
    // 到期退款（另一条终态路径）
    // ---------------------------------------------------------------------

    /// @notice 截止时间之后，仅发送者本人可收回未被领取的资金。
    /// @dev    严格检查顺序：存在 → 仍锁定 → 调用者是发送者 → 已到期。
    function refund(uint256 lockId) external nonReentrant {
        Lock storage l = _locks[lockId];
        if (l.state == State.NONE) revert UnknownLock();
        if (l.state != State.LOCKED) revert NotLocked();
        if (msg.sender != l.sender) revert NotSender();
        // 时间是“恰好到期即可退”：block.timestamp >= timelock 时退款成功。
        if (block.timestamp < l.timelock) revert StillLocked();

        l.state = State.REFUNDED;
        emit Refunded(lockId);

        if (!IERC20(l.token).transfer(l.sender, l.amount)) {
            revert("HashTimeLock: token transfer failed");
        }
    }

    // ---------------------------------------------------------------------
    // 只读视图
    // ---------------------------------------------------------------------

    /// @notice 查询单锁的全部字段。
    function getLock(uint256 lockId) external view returns (Lock memory) {
        return _locks[lockId];
    }

    /// @notice 便捷状态查询。
    function getState(uint256 lockId) external view returns (State) {
        return _locks[lockId].state;
    }
}
