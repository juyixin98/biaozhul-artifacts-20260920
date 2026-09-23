// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @title HashTimeLock
/// @notice 测试资产（ERC-20）哈希时间锁合约。同一合约代码分别部署在两条本地演示链上，
///         两条腿共享同一个哈希锁（keccak256 原像），构成哈希时间锁原子兑换（HTLC）。
/// @dev    状态机：ABSENT（无锁） -> LOCKED -> CLAIMED 或 REFUNDED（互斥终态，不可逆）。
///         领取条件：给出正确原像且 block.timestamp < timelock；
///         退款条件：block.timestamp >= timelock。
///         合约对外部代币转账采用「检查-生效-交互」顺序，并内置重入互斥锁（双重防护）。
contract HashTimeLock {
    enum State {
        ABSENT, // 0: 不存在该锁
        LOCKED, // 1: 已锁定，唯一的中间态
        CLAIMED, // 2: 终态：已被接收者凭原像领取
        REFUNDED // 3: 终态：到期后已退款给锁定者
    }

    struct Lock {
        address sender; // 锁定资金、到期收款的一方
        address receiver; // 唯一的领取收款方（在 lock 时绑定）
        address token; // 被锁定的 ERC-20 测试资产
        uint256 amount; // 锁定金额
        bytes32 hashlock; // keccak256(preimage)，在 lock 时绑定
        uint256 timelock; // Unix 秒级截止时间，在 lock 时绑定
        State state;
    }

    /// @dev id => 锁记录。mapping 只能从 ABSENT 进入 LOCKED 一次。
    mapping(bytes32 id => Lock) public locks;

    uint256 private _nonce;
    bool private _entered; // 重入互斥锁

    event Locked(
        bytes32 indexed id,
        address indexed sender,
        address indexed receiver,
        address token,
        uint256 amount,
        bytes32 hashlock,
        uint256 timelock
    );
    event Claimed(bytes32 indexed id, bytes32 preimage);
    event Refunded(bytes32 indexed id);

    error InvalidReceiver();
    error InvalidAmount();
    error InvalidTimelock();
    error NotLocked();
    error TooEarly();
    error TooLate();
    error WrongPreimage();
    error ReentrantCall();
    error TransferFailed();

    modifier nonReentrant() {
        if (_entered) revert ReentrantCall();
        _entered = true;
        _;
        _entered = false;
    }

    /// @notice 锁定一笔 ERC-20：资金被托管到本合约，锁与接收者/摘要/金额/截止时间绑定。
    /// @dev    调用前必须对本合约完成不少于 amount 的 ERC-20 approve。
    /// @param receiver 唯一领取收款方
    /// @param token    测试 ERC-20 地址
    /// @param amount   金额（最小单位）
    /// @param hashlock keccak256(preimage)
    /// @param timelock 截止时间（Unix 秒），必须晚于当前区块时间
    /// @return id 锁 ID（含 nonce 与 chainid，防止同一笔参数碰撞）
    function lock(address receiver, address token, uint256 amount, bytes32 hashlock, uint256 timelock)
        external
        nonReentrant
        returns (bytes32 id)
    {
        if (receiver == address(0)) revert InvalidReceiver();
        if (amount == 0) revert InvalidAmount();
        if (timelock <= block.timestamp) revert InvalidTimelock();

        // 先托管资金（交互）。失败必须回滚，不能先写状态。
        if (!IERC20Like(token).transferFrom(msg.sender, address(this), amount)) {
            revert TransferFailed();
        }

        unchecked {
            id = keccak256(abi.encode(msg.sender, receiver, token, amount, hashlock, timelock, _nonce++, block.chainid));
        }

        locks[id] = Lock({
            sender: msg.sender,
            receiver: receiver,
            token: token,
            amount: amount,
            hashlock: hashlock,
            timelock: timelock,
            state: State.LOCKED
        });

        emit Locked(id, msg.sender, receiver, token, amount, hashlock, timelock);
    }

    /// @notice 在截止前凭原像领取；资金只转给锁绑定的 receiver。
    /// @dev    任何人都可提交原像（原像经链上事件公开），但收款方恒为 receiver。
    function claim(bytes32 id, bytes32 preimage) external nonReentrant {
        Lock storage l = locks[id];
        if (l.state != State.LOCKED) revert NotLocked();
        // 严格小于：block.timestamp == timelock 即视为已到期。
        if (block.timestamp >= l.timelock) revert TooLate();
        if (keccak256(abi.encodePacked(preimage)) != l.hashlock) revert WrongPreimage();

        // 先把状态置为终态（生效），再做外部转账（交互）——CEI。
        l.state = State.CLAIMED;
        emit Claimed(id, preimage);

        if (!IERC20Like(l.token).transfer(l.receiver, l.amount)) revert TransferFailed();
    }

    /// @notice 到期后退款给锁定者；任何人都可触发，资金恒退回 sender。
    function refund(bytes32 id) external nonReentrant {
        Lock storage l = locks[id];
        if (l.state != State.LOCKED) revert NotLocked();
        // 与 claim 边界互补：到期瞬间（==）即可退款。
        if (block.timestamp < l.timelock) revert TooEarly();

        l.state = State.REFUNDED;
        emit Refunded(id);

        if (!IERC20Like(l.token).transfer(l.sender, l.amount)) revert TransferFailed();
    }

    function stateOf(bytes32 id) external view returns (State) {
        return locks[id].state;
    }

    /// @dev 供链下/测试统一计算哈希锁。
    function hashOf(bytes32 preimage) external pure returns (bytes32) {
        return keccak256(abi.encodePacked(preimage));
    }
}

interface IERC20Like {
    function transfer(address to, uint256 amount) external returns (bool);

    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}
