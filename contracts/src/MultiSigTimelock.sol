// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title MultiSigTimelock
/// @notice 多签延时执行器：
/// - 操作哈希绑定 target / data / value / nonce / validUntil
/// - 预置签名人 + 阈值，按 EIP-712 去重签名（同一签名人对同一操作只计一次）
/// - 达到阈值后操作进入调度态，仍需等待 delay 秒才能执行
/// - 每个操作至多成功执行一次；目标调用失败时按 retryCooldown 规则重试
/// - 签名人/阈值变更只能通过本钱包自身提案执行（自调用管理函数）
contract MultiSigTimelock {
    // ---------------------------------------------------------------------
    // 类型
    // ---------------------------------------------------------------------

    struct Operation {
        address target; // 目标合约
        bytes data; // 调用数据
        uint256 value; // 附带原生币
        uint256 nonce; // 防重放 nonce（调度后即被消费）
        uint64 validUntil; // 操作签名有效期截止时间戳（秒）
        bool scheduled; // 是否已达到阈值进入调度
        bool executed; // 是否成功执行过（终态）
        uint64 readyAt; // 调度时间 + delay，早于此时间不能执行
        uint64 lastFailureAt; // 最近一次目标调用失败的时间戳
        uint16 approvalCount; // 去重后的有效签名数
    }

    // ---------------------------------------------------------------------
    // 存储
    // ---------------------------------------------------------------------

    bytes32 public constant DOMAIN_TYPEHASH =
        keccak256(
            "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
        );

    bytes32 public constant OPERATION_TYPEHASH =
        keccak256(
            "Operation(address target,bytes data,uint256 value,uint256 nonce,uint64 validUntil)"
        );

    bytes32 public constant NAME_HASH = keccak256(bytes("MultiSigTimelock"));
    bytes32 public constant VERSION_HASH = keccak256(bytes("1"));

    mapping(address => bool) public isSigner;
    address[] public signers;
    uint16 public threshold;

    uint64 public delay; // 调度后必须等待的秒数
    uint64 public retryCooldown; // 目标失败后再次执行所需冷却秒数

    mapping(bytes32 => Operation) internal _ops;
    /// @dev 操作哈希 => 签名人 => 是否已计入，用于签名去重
    mapping(bytes32 => mapping(address => bool)) internal _approved;
    /// @dev 已消费 nonce，防止同一操作被重复调度
    mapping(uint256 => bool) public nonceUsed;

    // ---------------------------------------------------------------------
    // 事件
    // ---------------------------------------------------------------------

    event SignerConfigured(address[] signers, uint16 threshold);
    event DelayUpdated(uint64 delay, uint64 retryCooldown);
    event Approval(bytes32 indexed opHash, address indexed signer, uint16 count);
    event Scheduled(bytes32 indexed opHash, uint64 readyAt);
    event Executed(bytes32 indexed opHash, bytes returnData);
    event ExecutionFailed(bytes32 indexed opHash, bytes returnData, uint64 nextTryAfter);
    event Cancelled(bytes32 indexed opHash);

    // ---------------------------------------------------------------------
    // 错误
    // ---------------------------------------------------------------------

    error InvalidSigner();
    error InvalidSignature();
    error AlreadyApproved();
    error AlreadyScheduled();
    error AlreadyExecuted();
    error NotScheduled();
    error TimelockNotReady(uint64 readyAt);
    error RetryCooldownActive(uint64 nextTryAfter);
    error OperationExpired();
    error NonceAlreadyUsed();
    error Unauthorized();
    error InvalidThreshold();
    error EmptySigners();
    error DuplicateSigner();
    error ZeroAddress();

    // ---------------------------------------------------------------------
    // 构造 / 管理
    // ---------------------------------------------------------------------

    constructor(
        address[] memory _signers,
        uint16 _threshold,
        uint64 _delay,
        uint64 _retryCooldown
    ) {
        _configureSigners(_signers, _threshold);
        delay = _delay;
        retryCooldown = _retryCooldown;
        emit DelayUpdated(_delay, _retryCooldown);
    }

    /// @notice 更新签名人集合与阈值。只能由本合约自身调用
    ///         （即需走多签提案），不允许部署者直接修改。
    function configureSigners(address[] calldata _signers, uint16 _threshold) external {
        if (msg.sender != address(this)) revert Unauthorized();
        _configureSigners(_signers, _threshold);
    }

    /// @notice 更新延时参数。同样只能自调用。
    function setDelay(uint64 _delay, uint64 _retryCooldown) external {
        if (msg.sender != address(this)) revert Unauthorized();
        delay = _delay;
        retryCooldown = _retryCooldown;
        emit DelayUpdated(_delay, _retryCooldown);
    }

    function _configureSigners(address[] memory _signers, uint16 _threshold) internal {
        if (_signers.length == 0) revert EmptySigners();
        if (_threshold == 0 || uint256(_threshold) > _signers.length) revert InvalidThreshold();

        // 先清掉旧签名人标记
        for (uint256 i; i < signers.length; ++i) {
            isSigner[signers[i]] = false;
        }
        delete signers;

        for (uint256 i; i < _signers.length; ++i) {
            address s = _signers[i];
            if (s == address(0)) revert ZeroAddress();
            if (isSigner[s]) revert DuplicateSigner();
            isSigner[s] = true;
            signers.push(s);
        }
        threshold = _threshold;
        emit SignerConfigured(_signers, _threshold);
    }

    // ---------------------------------------------------------------------
    // 哈希与签名恢复
    // ---------------------------------------------------------------------

    /// @dev 计算操作哈希（不含 EIP-712 域名，便于跨链测试）。
    function hashOperation(
        address target,
        bytes calldata data,
        uint256 value,
        uint256 nonce,
        uint64 validUntil
    ) public view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(
                OPERATION_TYPEHASH,
                target,
                keccak256(data),
                value,
                nonce,
                validUntil
            )
        );
        return keccak256(abi.encodePacked("\x19\x01", _domainSeparator(), structHash));
    }

    function _domainSeparator() internal view returns (bytes32) {
        return
            keccak256(
                abi.encode(
                    DOMAIN_TYPEHASH,
                    NAME_HASH,
                    VERSION_HASH,
                    block.chainid,
                    address(this)
                )
            );
    }

    function domainSeparator() external view returns (bytes32) {
        return _domainSeparator();
    }

    /// @dev 从 65 字节签名恢复签名人，兼容 EIP-2098 紧凑格式（64 字节）。
    function _recover(bytes32 digest, bytes memory sig) internal pure returns (address) {
        if (sig.length == 65) {
            bytes32 r;
            bytes32 s;
            uint8 v;
            assembly {
                r := mload(add(sig, 32))
                s := mload(add(sig, 64))
                v := byte(0, mload(add(sig, 96)))
            }
            return ecrecover(digest, v, r, s);
        } else if (sig.length == 64) {
            bytes32 r;
            bytes32 vs;
            assembly {
                r := mload(add(sig, 32))
                vs := mload(add(sig, 64))
            }
            bytes32 s = vs & bytes32(0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff);
            uint8 v = uint8((uint256(vs) >> 255) + 27);
            return ecrecover(digest, v, r, s);
        }
        return address(0);
    }

    // ---------------------------------------------------------------------
    // 提案 / 签名 / 调度
    // ---------------------------------------------------------------------

    /// @notice 一次性提交多份签名。签名顺序任意（乱序安全）：
    /// 每份签名独立恢复并去重，达到阈值即调度。
    /// 可对尚未调度的操作重复调用追加签名。
    function approve(
        address target,
        bytes calldata data,
        uint256 value,
        uint256 nonce,
        uint64 validUntil,
        bytes[] calldata signatures
    ) external returns (bytes32 opHash, uint16 count) {
        if (validUntil != 0 && block.timestamp > validUntil) revert OperationExpired();
        if (nonceUsed[nonce]) revert NonceAlreadyUsed();

        opHash = hashOperation(target, data, value, nonce, validUntil);
        Operation storage op = _ops[opHash];

        // 首次见到该操作时固化操作内容
        if (!op.scheduled && op.target == address(0) && op.approvalCount == 0) {
            op.target = target;
            op.data = data;
            op.value = value;
            op.nonce = nonce;
            op.validUntil = validUntil;
        }

        for (uint256 i; i < signatures.length; ++i) {
            address signer = _recover(opHash, signatures[i]);
            if (signer == address(0) || !isSigner[signer]) revert InvalidSignature();
            if (_approved[opHash][signer]) continue; // 重复签名直接忽略，去重
            _approved[opHash][signer] = true;
            unchecked {
                op.approvalCount++;
            }
            emit Approval(opHash, signer, op.approvalCount);
            if (!op.scheduled && op.approvalCount >= threshold) {
                op.scheduled = true;
                op.readyAt = uint64(block.timestamp) + delay;
                nonceUsed[nonce] = true;
                emit Scheduled(opHash, op.readyAt);
                break;
            }
        }
        return (opHash, op.approvalCount);
    }

    /// @notice 执行（含重试）。规则：
    /// 1. 必须已调度；2. 未成功执行过；3. 未过 validUntil；
    /// 4. 当前时间 >= readyAt；5. 若上次失败，需间隔 retryCooldown。
    /// 目标调用失败不改变终态，可在冷却后重试。
    function execute(
        address target,
        bytes calldata data,
        uint256 value,
        uint256 nonce,
        uint64 validUntil
    ) external returns (bytes memory) {
        bytes32 opHash = hashOperation(target, data, value, nonce, validUntil);
        Operation storage op = _ops[opHash];
        if (op.executed) revert AlreadyExecuted();
        if (!op.scheduled) revert NotScheduled();
        if (op.validUntil != 0 && block.timestamp > op.validUntil) revert OperationExpired();
        if (block.timestamp < op.readyAt) revert TimelockNotReady(op.readyAt);
        if (op.lastFailureAt != 0 && block.timestamp < op.lastFailureAt + retryCooldown) {
            revert RetryCooldownActive(op.lastFailureAt + retryCooldown);
        }

        (bool ok, bytes memory ret) = target.call{value: value}(data);
        if (ok) {
            op.executed = true; // 成功即终态，保证至多执行一次
            emit Executed(opHash, ret);
            return ret;
        }
        op.lastFailureAt = uint64(block.timestamp);
        emit ExecutionFailed(opHash, ret, op.lastFailureAt + retryCooldown);
        // 失败不消耗执行次数：冷却过后可再次 execute
        return new bytes(0);
    }

    /// @notice 调度后、执行前可由任意签名人取消（延时窗口的安全用途之一）。
    function cancel(
        address target,
        bytes calldata data,
        uint256 value,
        uint256 nonce,
        uint64 validUntil
    ) external {
        if (!isSigner[msg.sender]) revert InvalidSigner();
        bytes32 opHash = hashOperation(target, data, value, nonce, validUntil);
        Operation storage op = _ops[opHash];
        if (!op.scheduled || op.executed) {
            if (!op.scheduled) revert NotScheduled();
            revert AlreadyExecuted();
        }
        op.executed = true; // 取消即终态，不可再执行
        emit Cancelled(opHash);
    }

    // ---------------------------------------------------------------------
    // 视图
    // ---------------------------------------------------------------------

    function getOperation(bytes32 opHash) external view returns (Operation memory) {
        return _ops[opHash];
    }

    function hasApproved(bytes32 opHash, address signer) external view returns (bool) {
        return _approved[opHash][signer];
    }

    function signerCount() external view returns (uint256) {
        return signers.length;
    }

    function getSigners() external view returns (address[] memory) {
        return signers;
    }

    receive() external payable {}
}
