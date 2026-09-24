// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title MultisigTimelock
/// @notice Local multi-signature executor with a mandatory timelock window.
/// @dev A *proposal* binds (target, value, dataHash, nonce, deadline) into a
///      stable op id. What signers actually sign (the EIP-712 digest) also binds
///      `configVersion`, so signatures are valid only for the current signer
///      set. Signers each approve a proposal with a signature; signatures are
///      de-duplicated by recovered signer address; once the number of distinct,
///      currently-authorised signers reaches `threshold`, the operation becomes
///      SCHEDULED and `timelockSeconds` must elapse before anyone can execute it.
///
///      Operations are content-addressed by their hash, so the same (target, data,
///      nonce, deadline) always maps to the same op id. The nonce is claimed by the
///      proposal when it becomes SCHEDULED.
///
///      Retry rule (see `execute`): when the target call fails, the op stays in
///      SCHEDULED and `failures` increments. A new attempt is allowed only after
///      `retryCooldownSeconds`; after `maxFailures` failed attempts the op is
///      permanently FAILED. A successful call executes exactly once and the op
///      becomes EXECUTED; any later call with the same id reverts AlreadyTerminal.
contract MultisigTimelock {
    // -------------------------------------------------------------------------
    // EIP-712 typed-data
    // -------------------------------------------------------------------------

    bytes32 private constant OP_TYPEHASH =
        keccak256(
            "Op(address target,uint256 value,bytes32 dataHash,uint256 nonce,uint256 deadline,uint256 configVersion)"
        );

    /// @dev Identity hash: same fields as Op minus configVersion.
    bytes32 private constant OP_ID_TYPEHASH =
        keccak256(
            "OpId(address target,uint256 value,bytes32 dataHash,uint256 nonce,uint256 deadline)"
        );

    bytes32 private constant SET_SIGNERS_TYPEHASH =
        keccak256(
            "SetSigners(address[] signers,uint256 threshold,uint256 nonce,uint256 deadline,uint256 configVersion)"
        );

    bytes32 private constant DOMAIN_TYPEHASH =
        keccak256(
            "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
        );

    // -------------------------------------------------------------------------
    // Types
    // -------------------------------------------------------------------------

    enum State {
        None, // 0 - never seen
        Proposed, // 1 - waiting for signatures
        Scheduled, // 2 - threshold met, waiting out timelock
        Executed, // 3 - succeeded exactly once (terminal)
        Failed, // 4 - exhausted retries (terminal)
        Void, // 5 - expired before execution (terminal)
        Invalidated // 6 - signer set changed before execution (terminal)
    }

    struct Op {
        address target;
        uint96 value;
        bytes32 dataHash;
        uint64 nonce;
        uint64 deadline;
        uint64 configVersion; // signer set version this op was signed under
        State state; // 1 byte
        uint64 approvedAt; // block.timestamp of the threshold approval
        uint64 lastAttemptAt; // block.timestamp of the last failed call
        uint16 failures; // number of failed target calls
        mapping(address => bool) approvals; // signer => has approved this op
    }

    // -------------------------------------------------------------------------
    // Governance state
    // -------------------------------------------------------------------------

    address[] private _signerList;
    mapping(address => bool) public isSigner;
    uint256 public threshold;
    uint64 public configVersion;

    /// @notice Latest claimed operation nonce. Valid op nonces are 0..nonce.
    uint64 public nonce;

    uint64 public immutable timelockSeconds;
    uint16 public immutable maxFailures;
    uint64 public immutable retryCooldownSeconds;

    bytes32 private immutable _domainSeparator;
    uint256 private _locked = 1;

    mapping(bytes32 => Op) private _ops;
    /// @notice Ids of every non-terminal operation ever proposed (includes old
    ///         versions of an id that was reset by a signer-set change).
    bytes32[] private _liveOps;

    // -------------------------------------------------------------------------
    // Events
    // -------------------------------------------------------------------------

    event Proposed(bytes32 indexed id, address indexed target, uint64 nonce);
    event Approved(bytes32 indexed id, address indexed signer);
    event Scheduled(bytes32 indexed id, uint64 executeAfter);
    event Executed(bytes32 indexed id, bytes returnData);
    event CallFailed(bytes32 indexed id, uint16 failures, bytes returnData);
    event RetriesExhausted(bytes32 indexed id);
    event Voided(bytes32 indexed id);
    event Invalidated(bytes32 indexed id, uint64 newConfigVersion);
    event SignersChanged(address[] signers, uint256 threshold, uint64 configVersion);

    // -------------------------------------------------------------------------
    // Errors
    // -------------------------------------------------------------------------

    error ZeroAddress();
    error EmptySigners();
    error ThresholdOutOfRange(uint256 threshold, uint256 signerCount);
    error DuplicateSigner(address signer);
    error NonceUsed(uint64 nonce);
    error DeadlinePassed(uint256 deadline);
    error UnknownOp(bytes32 id);
    error AlreadyTerminal(bytes32 id, State state);
    error DuplicateSignature(address signer);
    error BadSignature(uint256 index);
    error ThresholdNotMet(uint256 got, uint256 need);
    error TimelockActive(uint256 readyAt, uint256 nowTs);
    error RetryCooldownActive(uint256 nextAttemptAt, uint256 nowTs);
    error ValueTooLarge(uint256 value);
    error Reentrant();

    modifier nonReentrant() {
        if (_locked != 1) revert Reentrant();
        _locked = 2;
        _;
        _locked = 1;
    }

    // -------------------------------------------------------------------------
    // Construction
    // -------------------------------------------------------------------------

    constructor(
        address[] memory initialSigners,
        uint256 initialThreshold,
        uint64 timelock,
        uint16 maxFails,
        uint64 retryCooldown
    ) {
        _setSignersData(initialSigners, initialThreshold);
        timelockSeconds = timelock;
        maxFailures = maxFails;
        retryCooldownSeconds = retryCooldown;

        bytes32 nameHash = keccak256(bytes("MultisigTimelock"));
        bytes32 versionHash = keccak256(bytes("1"));
        _domainSeparator = keccak256(
            abi.encode(DOMAIN_TYPEHASH, nameHash, versionHash, block.chainid, address(this))
        );
    }

    // -------------------------------------------------------------------------
    // Proposal / approval
    // -------------------------------------------------------------------------

    /// @notice Propose an operation and submit one batch of signatures at once.
    /// @dev Order of `signatures` does not matter: each is recovered and counted
    ///      by signer address. The same signer appearing twice in the batch makes
    ///      the whole call revert with DuplicateSignature.
    function propose(
        address target,
        uint256 value,
        bytes calldata data,
        uint64 opNonce,
        uint64 deadline,
        bytes[] calldata signatures
    ) external returns (bytes32 id) {
        if (target == address(0)) revert ZeroAddress();
        if (opNonce != nonce) revert NonceUsed(opNonce);
        if (deadline <= block.timestamp) revert DeadlinePassed(deadline);
        // uint96 holds ~7.9e28 ETH; the explicit cast reverts on overflow.
        if (value > uint256(type(uint96).max)) revert ValueTooLarge(value);

        id = opId(target, value, keccak256(data), opNonce, deadline);

        Op storage op = _ops[id];
        if (op.state == State.None) {
            op.target = target;
            op.value = uint96(value);
            op.dataHash = keccak256(data);
            op.nonce = opNonce;
            op.deadline = deadline;
            op.configVersion = configVersion;
            op.state = State.Proposed;
            _liveOps.push(id);
            emit Proposed(id, target, opNonce);
        } else {
            _requireActive(op, id);
        }

        _applyApprovals(op, id, target, value, op.dataHash, opNonce, deadline, signatures);
    }

    /// @notice Add signatures to an already-proposed op.
    function approve(
        address target,
        uint256 value,
        bytes32 dataHash,
        uint64 opNonce,
        uint64 deadline,
        bytes[] calldata signatures
    ) external returns (bytes32 id) {
        id = opId(target, value, dataHash, opNonce, deadline);
        Op storage op = _ops[id];
        if (op.state == State.None) revert UnknownOp(id);
        _requireActive(op, id);
        _applyApprovals(op, id, target, value, dataHash, opNonce, deadline, signatures);
    }

    /// @notice Anyone may void an op whose deadline has passed without executing.
    function voidExpired(bytes32 id) external {
        Op storage op = _ops[id];
        if (op.state == State.None) revert UnknownOp(id);
        if (
            op.state == State.Executed ||
            op.state == State.Failed ||
            op.state == State.Invalidated
        ) {
            revert AlreadyTerminal(id, op.state);
        }
        if (op.state == State.Void) revert AlreadyTerminal(id, State.Void);
        if (block.timestamp <= op.deadline) revert DeadlinePassed(op.deadline);
        op.state = State.Void;
        emit Voided(id);
    }

    // -------------------------------------------------------------------------
    // Execution
    // -------------------------------------------------------------------------

    /// @notice Execute a scheduled op. The exact `data` must be supplied; its hash
    ///         must match what was signed. Callable by anyone once the timelock is
    ///         over. Reverts on success-path misuse; failed target calls are caught
    ///         and follow the documented retry rule.
    function execute(
        address target,
        uint256 value,
        bytes calldata data,
        uint64 opNonce,
        uint64 deadline
    ) external nonReentrant returns (bytes memory) {
        bytes32 id = opId(target, value, keccak256(data), opNonce, deadline);
        Op storage op = _ops[id];
        if (op.state == State.None) revert UnknownOp(id);
        if (
            op.state == State.Executed ||
            op.state == State.Failed ||
            op.state == State.Void ||
            op.state == State.Invalidated
        ) revert AlreadyTerminal(id, op.state);
        // Note: keccak256(data) is already bound into `id`, so tampered calldata
        // resolves to a different (Unknown) op rather than reaching a mismatch.
        // Expiry only reverts here (a revert could not persist a state change);
        // anyone can then mark the op Void explicitly via voidExpired().
        if (block.timestamp > op.deadline) revert DeadlinePassed(op.deadline);

        // First attempt is gated by the timelock. After a failure the op stays
        // SCHEDULED and further attempts also respect the retry cooldown.
        uint256 readyAt = op.approvedAt + timelockSeconds;
        if (op.failures > 0) {
            readyAt = op.lastAttemptAt + retryCooldownSeconds;
        }
        if (block.timestamp < readyAt) {
            if (op.failures > 0) revert RetryCooldownActive(readyAt, block.timestamp);
            revert TimelockActive(readyAt, block.timestamp);
        }

        op.lastAttemptAt = uint64(block.timestamp);

        (bool ok, bytes memory ret) = target.call{value: value}(data);
        if (ok) {
            op.state = State.Executed;
            // Nonce claimed at schedule time, nothing else to do.
            emit Executed(id, ret);
            return ret;
        }

        op.failures += 1;
        emit CallFailed(id, op.failures, ret);
        if (op.failures >= maxFailures) {
            op.state = State.Failed;
            emit RetriesExhausted(id);
        }
        // Stay SCHEDULED (if not exhausted): another execute() may be called
        // after retryCooldownSeconds.
        return ret;
    }

    // -------------------------------------------------------------------------
    // Signer-set management (itself a threshold+timelock... no: signer changes
    // take effect immediately once threshold signatures are collected, and reset
    // every in-flight op so stale signatures cannot schedule them).
    // -------------------------------------------------------------------------

    /// @notice Replace the signer set / threshold. Requires `threshold` valid
    ///         signatures over the new set under the CURRENT config version.
    /// @dev Every non-terminal operation is reset to Proposed with its approvals
    ///      cleared and configVersion stamped to the new version. Because the
    ///      EIP-712 payload embeds configVersion, signatures produced under the
    ///      old set can never be replayed against the new set. Scheduled-but-not-
    ///      executed ops must be re-approved and wait out the timelock again.
    function setSigners(
        address[] calldata newSigners,
        uint256 newThreshold,
        uint64 opNonce,
        uint64 deadline,
        bytes[] calldata signatures
    ) external {
        if (opNonce != nonce) revert NonceUsed(opNonce);
        if (deadline <= block.timestamp) revert DeadlinePassed(deadline);

        bytes32 structHash = keccak256(
            abi.encode(
                SET_SIGNERS_TYPEHASH,
                keccak256(abi.encodePacked(newSigners)),
                newThreshold,
                opNonce,
                deadline,
                configVersion
            )
        );
        bytes32 digest = _toTypedDataDigest(structHash);

        _requireUniqueThresholdSignatures(digest, signatures);

        unchecked {
            configVersion += 1;
        }
        // The signer-change op claims the nonce just like a scheduled call.
        nonce = opNonce + 1;
        _setSignersData(newSigners, newThreshold);
        _resetLiveOps();

        emit SignersChanged(newSigners, newThreshold, configVersion);
    }

    // -------------------------------------------------------------------------
    // Views
    // -------------------------------------------------------------------------

    function domainSeparator() external view returns (bytes32) {
        return _domainSeparator;
    }

    function signers() external view returns (address[] memory) {
        return _signerList;
    }

    function liveOpsLength() external view returns (uint256) {
        return _liveOps.length;
    }

    /// @notice Stable operation identity. Binds target/value/data/nonce/deadline
    ///         only, so the same op stays addressable (and visibly terminal) even
    ///         after a signer-set change. It is NOT what signers sign.
    function opId(
        address target,
        uint256 value,
        bytes32 dataHash,
        uint64 opNonce,
        uint64 deadline
    ) public view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(OP_ID_TYPEHASH, target, value, dataHash, opNonce, deadline)
        );
        return _toTypedDataDigest(structHash);
    }

    /// @notice The EIP-712 digest signers actually sign for an op. It embeds the
    ///         current configVersion, so a signature is only valid for the signer
    ///         set active when it was produced.
    function digestFor(
        address target,
        uint256 value,
        bytes32 dataHash,
        uint64 opNonce,
        uint64 deadline
    ) external view returns (bytes32) {
        return
            _toTypedDataDigest(
                keccak256(
                    abi.encode(
                        OP_TYPEHASH,
                        target,
                        value,
                        dataHash,
                        opNonce,
                        deadline,
                        configVersion
                    )
                )
            );
    }

    function digestForSetSigners(
        address[] calldata newSigners,
        uint256 newThreshold,
        uint64 opNonce,
        uint64 deadline
    ) external view returns (bytes32) {
        return
            _toTypedDataDigest(
                keccak256(
                    abi.encode(
                        SET_SIGNERS_TYPEHASH,
                        keccak256(abi.encodePacked(newSigners)),
                        newThreshold,
                        opNonce,
                        deadline,
                        configVersion
                    )
                )
            );
    }

    /// @notice Full snapshot of an op. `ready`/`readyAt` describe the next
    ///         permitted execution time (timelock or retry cooldown).
    function getOp(
        bytes32 id
    )
        external
        view
        returns (
            address target,
            uint256 value,
            bytes32 dataHash,
            uint64 opNonce,
            uint64 deadline,
            uint64 opConfigVersion,
            State state,
            uint64 approvedAt,
            uint64 lastAttemptAt,
            uint16 failures,
            uint256 approvalCount,
            bool ready,
            uint256 readyAt
        )
    {
        Op storage op = _ops[id];
        target = op.target;
        value = op.value;
        dataHash = op.dataHash;
        opNonce = op.nonce;
        deadline = op.deadline;
        opConfigVersion = op.configVersion;
        state = op.state;
        approvedAt = op.approvedAt;
        lastAttemptAt = op.lastAttemptAt;
        failures = op.failures;
        approvalCount = _countApprovals(id);
        if (op.state == State.Scheduled) {
            readyAt = op.failures > 0
                ? uint256(op.lastAttemptAt) + retryCooldownSeconds
                : uint256(op.approvedAt) + timelockSeconds;
            ready = block.timestamp >= readyAt && block.timestamp <= op.deadline;
        }
    }

    function hasApproved(bytes32 id, address signer) external view returns (bool) {
        return _ops[id].approvals[signer];
    }

    receive() external payable {}

    // -------------------------------------------------------------------------
    // Internals
    // -------------------------------------------------------------------------

    function _requireActive(Op storage op, bytes32 id) private view {
        if (
            op.state == State.Executed ||
            op.state == State.Failed ||
            op.state == State.Void ||
            op.state == State.Invalidated
        ) revert AlreadyTerminal(id, op.state);
    }

    function _applyApprovals(
        Op storage op,
        bytes32 id,
        address target,
        uint256 value,
        bytes32 dataHash,
        uint64 opNonce,
        uint64 deadline,
        bytes[] calldata signatures
    ) private {
        // Signatures are checked against the configVersion the op was created
        // under; a signer-set change invalidates the op before this can matter.
        bytes32 digest = _toTypedDataDigest(
            keccak256(
                abi.encode(
                    OP_TYPEHASH,
                    target,
                    value,
                    dataHash,
                    opNonce,
                    deadline,
                    op.configVersion
                )
            )
        );

        for (uint256 i = 0; i < signatures.length; i++) {
            address signer = _recover(digest, signatures[i], i);
            if (!isSigner[signer]) revert BadSignature(i);
            if (op.approvals[signer]) revert DuplicateSignature(signer);
            op.approvals[signer] = true;
            emit Approved(id, signer);
        }
        if (op.state == State.Proposed) {
            uint256 total = _countApprovals(id);
            if (total >= threshold) {
                op.state = State.Scheduled;
                op.approvedAt = uint64(block.timestamp);
                // Claim the nonce at scheduling time, so a competing proposal for
                // the same nonce cannot schedule afterwards.
                nonce = opNonce + 1;
                emit Scheduled(id, op.approvedAt);
            }
        }
    }

    function _requireUniqueThresholdSignatures(
        bytes32 digest,
        bytes[] calldata signatures
    ) private view {
        address[] memory seen = new address[](signatures.length);
        uint256 count;
        for (uint256 i = 0; i < signatures.length; i++) {
            address signer = _recover(digest, signatures[i], i);
            if (!isSigner[signer]) revert BadSignature(i);
            for (uint256 j = 0; j < count; j++) {
                if (seen[j] == signer) revert DuplicateSignature(signer);
            }
            seen[count] = signer;
            count++;
        }
        if (count < threshold) revert ThresholdNotMet(count, threshold);
    }

    function _countApprovals(bytes32 id) private view returns (uint256 count) {
        address[] memory list = _signerList;
        for (uint256 i = 0; i < list.length; i++) {
            if (_ops[id].approvals[list[i]]) count++;
        }
    }

    /// @dev After a signer-set change every non-terminal op is permanently
    ///      INVALIDATED: its approvals were produced under the old signer set and
    ///      must never schedule or execute anything. Because op ids are stable
    ///      (configVersion is not part of the id), the terminal row also blocks
    ///      any later re-proposal of the exact same (target, value, data, nonce,
    ///      deadline) tuple. The nonce was already claimed, so a fresh proposal
    ///      uses the next nonce and gets a fresh id.
    function _resetLiveOps() private {
        for (uint256 i = 0; i < _liveOps.length; i++) {
            bytes32 id = _liveOps[i];
            Op storage op = _ops[id];
            if (
                op.state == State.Executed ||
                op.state == State.Failed ||
                op.state == State.Void ||
                op.state == State.Invalidated
            ) continue;

            op.state = State.Invalidated;
            emit Invalidated(id, configVersion);
        }
        delete _liveOps;
    }

    function _setSignersData(address[] memory list, uint256 thr) private {
        if (list.length == 0) revert EmptySigners();
        if (thr == 0 || thr > list.length) revert ThresholdOutOfRange(thr, list.length);

        // Clear the old mapping.
        for (uint256 i = 0; i < _signerList.length; i++) {
            isSigner[_signerList[i]] = false;
        }
        delete _signerList;

        for (uint256 i = 0; i < list.length; i++) {
            if (list[i] == address(0)) revert ZeroAddress();
            if (isSigner[list[i]]) revert DuplicateSigner(list[i]);
            isSigner[list[i]] = true;
            _signerList.push(list[i]);
        }
        threshold = thr;
    }

    function _recover(
        bytes32 digest,
        bytes calldata signature,
        uint256 index
    ) private pure returns (address) {
        if (signature.length != 65) revert BadSignature(index);
        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly {
            r := calldataload(signature.offset)
            s := calldataload(add(signature.offset, 32))
            v := byte(0, calldataload(add(signature.offset, 64)))
        }
        if (v < 27) v += 27;
        if (v != 27 && v != 28) revert BadSignature(index);
        // Reject high-s / non-canonical signatures (ECDSA malleability).
        if (uint256(s) > 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0) {
            revert BadSignature(index);
        }
        address signer = ecrecover(digest, v, r, s);
        if (signer == address(0)) revert BadSignature(index);
        return signer;
    }

    function _toTypedDataDigest(bytes32 structHash) private view returns (bytes32) {
        return keccak256(abi.encodePacked("\x19\x01", _domainSeparator, structHash));
    }
}
