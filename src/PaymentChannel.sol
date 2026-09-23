// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IERC20Like {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}

/// @title PaymentChannel — 单向测试代币支付通道的链上状态裁决合约。
/// @notice 付款方 (payer) 锁定抵押代币；双方链下对「累计状态」共同签名，
///         状态绑定 通道ID / 链ID / 累计支付额 / 单调序号（EIP-712）。
///         任一方可用最新双方签名状态关闭通道并进入挑战期；挑战期内序号更高
///         且累计额不回退的状态可替换链上记录；挑战期到期后结算，收款方最多
///         拿走全部抵押额，余额退还付款方。结算只能执行一次。
///         本合约为独立的单向支付通道实现，不实现、也不声称兼容闪电网络协议。
contract PaymentChannel {
    // ------------------------------------------------------------------ types

    enum Status {
        Open, // 已开通，未进入关闭流程
        Closing, // 已关闭，挑战期进行中
        Settled // 已结算（终态）
    }

    struct Channel {
        address payer;
        address payee;
        uint256 collateral; // 抵押总额（结算支出的上限）
        uint256 amount; // 链上已记录的累计支付额
        uint64 nonce; // 链上已记录的单调序号
        uint64 challengeEnd; // 挑战期截止时刻（unix 秒）
        Status status;
    }

    /// @dev 双方链下签名的累计状态。amount 为「累计」支付额而非增量。
    struct State {
        bytes32 channelId;
        uint256 amount;
        uint64 nonce;
    }

    // ------------------------------------------------------------------ storage

    IERC20Like public immutable token;
    uint256 public immutable challengePeriod;

    mapping(bytes32 => Channel) public channels;

    // EIP-712
    bytes32 private constant EIP712_DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 private constant STATE_TYPEHASH = keccak256("State(bytes32 channelId,uint256 amount,uint64 nonce)");
    bytes32 private constant NAME_HASH = keccak256("PaymentChannel");
    bytes32 private constant VERSION_HASH = keccak256("1");

    bytes32 private immutable _cachedDomainSeparator;
    uint256 private immutable _cachedChainId;

    // secp256k1n/2 + 1，用于拒绝可延展签名
    uint256 private constant _S_UPPER = 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0;

    // ------------------------------------------------------------------ events

    event ChannelOpened(bytes32 indexed channelId, address indexed payer, address indexed payee, uint256 collateral);
    event ChannelClosed(bytes32 indexed channelId, uint64 nonce, uint256 amount, uint64 challengeEnd);
    event Challenged(bytes32 indexed channelId, uint64 nonce, uint256 amount);
    event Settled(bytes32 indexed channelId, uint256 payout, uint256 refund);

    // ------------------------------------------------------------------ errors

    error BadPayee();
    error ZeroCollateral();
    error ChannelExists();
    error ChannelNotFound();
    error NotOpen();
    error NotClosing();
    error NotParty();
    error BadSignature();
    error StaleNonce();
    error AmountRegression();
    error ChallengeActive();
    error ChallengeOver();
    error TransferFailed();

    // ------------------------------------------------------------------ ctor

    constructor(address token_, uint256 challengePeriod_) {
        require(token_ != address(0), "token = 0");
        require(challengePeriod_ > 0, "challengePeriod = 0");
        require(challengePeriod_ <= type(uint64).max, "challengePeriod too large");
        token = IERC20Like(token_);
        challengePeriod = challengePeriod_;
        _cachedChainId = block.chainid;
        _cachedDomainSeparator = _buildDomainSeparator();
    }

    // ------------------------------------------------------------------ views

    function getChannel(bytes32 channelId) external view returns (Channel memory) {
        return channels[channelId];
    }

    function domainSeparator() public view returns (bytes32) {
        if (block.chainid == _cachedChainId) return _cachedDomainSeparator;
        return _buildDomainSeparator();
    }

    /// @notice 状态的 EIP-712 签名摘要。链ID 与合约地址经域分隔符绑定进摘要。
    function hashState(State calldata s) public view returns (bytes32) {
        bytes32 structHash = keccak256(abi.encode(STATE_TYPEHASH, s.channelId, s.amount, s.nonce));
        return keccak256(abi.encodePacked("\x19\x01", domainSeparator(), structHash));
    }

    // ------------------------------------------------------------------ open

    /// @notice 付款方开通通道并锁定抵押代币。channelId 由 (payer, payee, salt) 推出。
    function openChannel(address payee, uint256 collateral, bytes32 salt) external returns (bytes32 channelId) {
        if (payee == address(0) || payee == msg.sender) revert BadPayee();
        if (collateral == 0) revert ZeroCollateral();
        channelId = keccak256(abi.encode(msg.sender, payee, salt));
        if (channels[channelId].payer != address(0)) revert ChannelExists();

        channels[channelId] = Channel({
            payer: msg.sender,
            payee: payee,
            collateral: collateral,
            amount: 0,
            nonce: 0,
            challengeEnd: 0,
            status: Status.Open
        });

        if (!token.transferFrom(msg.sender, address(this), collateral)) revert TransferFailed();
        emit ChannelOpened(channelId, msg.sender, payee, collateral);
    }

    // ------------------------------------------------------------------ close

    /// @notice 任一方提交一份双方签名的状态关闭通道，进入挑战期。
    ///         提交的状态可以是「旧状态」——对手方可在挑战期内用更高序号状态挑战。
    function closeChannel(State calldata s, bytes calldata sigPayer, bytes calldata sigPayee) external {
        Channel storage ch = channels[s.channelId];
        if (ch.payer == address(0)) revert ChannelNotFound();
        if (ch.status != Status.Open) revert NotOpen();
        if (msg.sender != ch.payer && msg.sender != ch.payee) revert NotParty();
        _verifyState(ch, s, sigPayer, sigPayee);
        if (s.nonce <= ch.nonce) revert StaleNonce();
        if (s.amount < ch.amount) revert AmountRegression();

        ch.nonce = s.nonce;
        ch.amount = s.amount;
        // challengePeriod 已在构造函数中限制为 uint64，block.timestamp 实际值远小于 2^64
        // forge-lint: disable-next-line(unsafe-typecast)
        ch.challengeEnd = uint64(block.timestamp) + uint64(challengePeriod);
        ch.status = Status.Closing;
        emit ChannelClosed(s.channelId, s.nonce, s.amount, ch.challengeEnd);
    }

    // ------------------------------------------------------------------ challenge

    /// @notice 挑战期内提交序号更高、累计额不回退的双方签名状态，覆盖链上记录。
    ///         不延长挑战期。任何人（通常是收款方）都可调用。
    function challenge(State calldata s, bytes calldata sigPayer, bytes calldata sigPayee) external {
        Channel storage ch = channels[s.channelId];
        if (ch.payer == address(0)) revert ChannelNotFound();
        if (ch.status != Status.Closing) revert NotClosing();
        if (block.timestamp >= ch.challengeEnd) revert ChallengeOver();
        _verifyState(ch, s, sigPayer, sigPayee);
        if (s.nonce <= ch.nonce) revert StaleNonce();
        if (s.amount < ch.amount) revert AmountRegression();

        ch.nonce = s.nonce;
        ch.amount = s.amount;
        emit Challenged(s.channelId, s.nonce, s.amount);
    }

    // ------------------------------------------------------------------ settle

    /// @notice 挑战期到期后结算：收款方获得 min(累计额, 抵押额)，余款退付款方。
    ///         状态置为 Settled，重复结算将被拒绝。
    function settle(bytes32 channelId) external {
        Channel storage ch = channels[channelId];
        if (ch.payer == address(0)) revert ChannelNotFound();
        if (ch.status != Status.Closing) revert NotClosing();
        if (block.timestamp < ch.challengeEnd) revert ChallengeActive();

        ch.status = Status.Settled;
        uint256 payout = ch.amount > ch.collateral ? ch.collateral : ch.amount;
        uint256 refund = ch.collateral - payout;

        if (payout > 0 && !token.transfer(ch.payee, payout)) revert TransferFailed();
        if (refund > 0 && !token.transfer(ch.payer, refund)) revert TransferFailed();
        emit Settled(channelId, payout, refund);
    }

    // ------------------------------------------------------------------ internal

    function _buildDomainSeparator() internal view returns (bytes32) {
        return keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, NAME_HASH, VERSION_HASH, block.chainid, address(this)));
    }

    function _verifyState(
        Channel storage ch,
        State calldata s,
        bytes calldata sigPayer,
        bytes calldata sigPayee
    ) internal view {
        bytes32 digest = hashState(s);
        if (_recover(digest, sigPayer) != ch.payer) revert BadSignature();
        if (_recover(digest, sigPayee) != ch.payee) revert BadSignature();
    }

    /// @dev ecrecover，带 v 校验与低 s 值（防可延展性）检查。
    function _recover(bytes32 digest, bytes calldata sig) internal pure returns (address) {
        if (sig.length != 65) revert BadSignature();
        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly {
            r := calldataload(sig.offset)
            s := calldataload(add(sig.offset, 32))
            v := byte(0, calldataload(add(sig.offset, 64)))
        }
        if (v < 27) v += 27;
        if (v != 27 && v != 28) revert BadSignature();
        if (uint256(s) > _S_UPPER) revert BadSignature();
        address signer = ecrecover(digest, v, r, s);
        if (signer == address(0)) revert BadSignature();
        return signer;
    }
}
