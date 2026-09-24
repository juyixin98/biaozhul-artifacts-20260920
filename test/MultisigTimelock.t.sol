// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {MultisigTimelock} from "../contracts/MultisigTimelock.sol";
import {Counter, FlakyTarget, AlwaysFail, ReentrantTarget} from "../contracts/Targets.sol";

/// @dev End-to-end contract-level acceptance tests for the multisig timelock.
/// Uses the well-known Anvil/forge test private keys (0xac0974... etc).
contract MultisigTimelockTest is Test {
    MultisigTimelock internal executor;
    Counter internal counter;

    uint256 internal constant PK0 = 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;
    uint256 internal constant PK1 = 0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d;
    uint256 internal constant PK2 = 0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a;
    uint256 internal constant PK3 = 0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6;
    uint256 internal constant PK4 = 0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a;

    address internal s0;
    address internal s1;
    address internal s2;
    address internal s3;
    address internal s4;

    uint64 internal constant TIMELOCK = 2;
    uint16 internal constant MAX_FAILURES = 2;
    uint64 internal constant RETRY_COOLDOWN = 1;
    uint64 internal constant HOUR = 3600;

    bytes32 internal constant ZERO_DATA_HASH = keccak256("");

    function setUp() public {
        s0 = vm.addr(PK0);
        s1 = vm.addr(PK1);
        s2 = vm.addr(PK2);
        s3 = vm.addr(PK3);
        s4 = vm.addr(PK4);

        address[] memory signers = new address[](3);
        signers[0] = s0;
        signers[1] = s1;
        signers[2] = s2;
        executor = new MultisigTimelock(signers, 2, TIMELOCK, MAX_FAILURES, RETRY_COOLDOWN);
        counter = new Counter();
    }

    // ---- helpers -----------------------------------------------------------

    function _deadline() internal view returns (uint64) {
        return uint64(block.timestamp + HOUR);
    }

    function _sign(bytes32 digest, uint256 pk) internal pure returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        return abi.encodePacked(r, s, v);
    }

    function _digest(
        address target,
        uint256 value,
        bytes32 dataHash,
        uint64 n,
        uint64 deadline
    ) internal view returns (bytes32) {
        return executor.digestFor(target, value, dataHash, n, deadline);
    }

    function _opId(
        address target,
        bytes memory data,
        uint64 n,
        uint64 deadline
    ) internal view returns (bytes32) {
        return executor.opId(target, 0, keccak256(data), n, deadline);
    }

    function _counterData(uint256 step, bytes32 tag) internal pure returns (bytes memory) {
        return abi.encodeCall(Counter.increment, (step, tag));
    }

    function _sigArray(
        bytes memory a,
        bytes memory b
    ) internal pure returns (bytes[] memory arr) {
        arr = new bytes[](2);
        arr[0] = a;
        arr[1] = b;
    }

    function _sigArray(
        bytes memory a,
        bytes memory b,
        bytes memory c
    ) internal pure returns (bytes[] memory arr) {
        arr = new bytes[](3);
        arr[0] = a;
        arr[1] = b;
        arr[2] = c;
    }

    function _proposeCounter(
        uint64 n,
        uint256 step,
        bytes32 tag,
        uint256 pkA,
        uint256 pkB
    ) internal returns (bytes32 id, bytes memory data, uint64 deadline) {
        deadline = _deadline();
        data = _counterData(step, tag);
        bytes32 digest = _digest(address(counter), 0, keccak256(data), n, deadline);
        bytes[] memory sigs = _sigArray(_sign(digest, pkA), _sign(digest, pkB));
        id = executor.propose(address(counter), 0, data, n, deadline, sigs);
    }

    // ---- acceptance tests --------------------------------------------------

    /// 1. Out-of-order signatures must still reach the threshold.
    function test_unorderedSignatures_schedules() public {
        uint64 deadline = _deadline();
        bytes memory data = _counterData(1, "unordered");
        uint64 n = executor.nonce();
        bytes32 digest = _digest(address(counter), 0, keccak256(data), n, deadline);

        // Submit in deliberately scrambled order: s2 then s0.
        bytes[] memory sigs = _sigArray(_sign(digest, PK2), _sign(digest, PK0));
        bytes32 id = executor.propose(address(counter), 0, data, n, deadline, sigs);

        (, , , , , , MultisigTimelock.State state, , , , uint256 approvals, , ) = executor
            .getOp(id);
        assertEq(uint8(state), uint8(MultisigTimelock.State.Scheduled), "scheduled");
        assertEq(approvals, 2, "two distinct signers");
        assertEq(executor.nonce(), n + 1, "nonce claimed");
    }

    /// 2a. Duplicate signer inside one batch reverts.
    function test_duplicateSignatureInBatch_reverts() public {
        uint64 deadline = _deadline();
        bytes memory data = _counterData(1, "dup");
        uint64 n = executor.nonce();
        bytes32 digest = _digest(address(counter), 0, keccak256(data), n, deadline);

        bytes[] memory sigs = _sigArray(_sign(digest, PK0), _sign(digest, PK0));
        vm.expectRevert(abi.encodeWithSelector(MultisigTimelock.DuplicateSignature.selector, s0));
        executor.propose(address(counter), 0, data, n, deadline, sigs);
    }

    /// 2b. Submitting the same signer's signature again via approve() reverts.
    function test_duplicateSignatureAcrossCalls_reverts() public {
        uint64 deadline = _deadline();
        bytes memory data = _counterData(1, "dup2");
        uint64 n = executor.nonce();
        bytes32 digest = _digest(address(counter), 0, keccak256(data), n, deadline);

        bytes[] memory one = new bytes[](1);
        one[0] = _sign(digest, PK0);
        bytes32 id = executor.propose(address(counter), 0, data, n, deadline, one);
        assertEq(uint8(_state(id)), uint8(MultisigTimelock.State.Proposed));

        bytes[] memory again = new bytes[](1);
        again[0] = _sign(digest, PK0);
        vm.expectRevert(abi.encodeWithSelector(MultisigTimelock.DuplicateSignature.selector, s0));
        executor.approve(
            address(counter),
            0,
            keccak256(data),
            n,
            deadline,
            again
        );

        // A second distinct signer pushes it to Scheduled.
        bytes[] memory two = new bytes[](1);
        two[0] = _sign(digest, PK1);
        executor.approve(address(counter), 0, keccak256(data), n, deadline, two);
        assertEq(uint8(_state(id)), uint8(MultisigTimelock.State.Scheduled));
    }

    /// 3. A signature from a non-signer reverts BadSignature.
    function test_nonSignerSignature_reverts() public {
        uint64 deadline = _deadline();
        bytes memory data = _counterData(1, "nonsigner");
        uint64 n = executor.nonce();
        bytes32 digest = _digest(address(counter), 0, keccak256(data), n, deadline);

        bytes[] memory sigs = _sigArray(_sign(digest, PK0), _sign(digest, PK4)); // PK4 not signer
        vm.expectRevert(abi.encodeWithSelector(MultisigTimelock.BadSignature.selector, 1));
        executor.propose(address(counter), 0, data, n, deadline, sigs);
    }

    /// 4. Execution before the timelock elapses reverts.
    function test_cannotExecuteBeforeTimelock() public {
        (bytes32 id, bytes memory data, uint64 deadline) = _proposeCounter(
            0,
            1,
            "early",
            PK0, PK1
        );
        vm.expectRevert();
        executor.execute(address(counter), 0, data, 0, deadline);
        // jump forward but not quite enough
        vm.warp(block.timestamp + TIMELOCK - 1);
        vm.expectRevert();
        executor.execute(address(counter), 0, data, 0, deadline);
        assertEq(uint8(_state(id)), uint8(MultisigTimelock.State.Scheduled));
    }

    /// 5. After the timelock, execution succeeds exactly once; double-execute
    ///    reverts and the target is invoked only one time.
    function test_executesExactlyOnce_afterTimelock() public {
        (bytes32 id, bytes memory data, uint64 deadline) = _proposeCounter(
            0,
            7,
            "once",
            PK0, PK1
        );
        vm.warp(block.timestamp + TIMELOCK);

        bytes memory ret = executor.execute(address(counter), 0, data, 0, deadline);
        assertEq(abi.decode(ret, (uint256)), 7);
        assertEq(counter.count(), 7);
        assertEq(counter.callCount(), 1);
        assertEq(counter.lastCaller(), address(executor), "executor is msg.sender (callback)");
        assertEq(uint8(_state(id)), uint8(MultisigTimelock.State.Executed));

        // Re-running the same op id must revert regardless of delay.
        vm.warp(block.timestamp + 100);
        vm.expectRevert(
            abi.encodeWithSelector(
                MultisigTimelock.AlreadyTerminal.selector,
                id,
                MultisigTimelock.State.Executed
            )
        );
        executor.execute(address(counter), 0, data, 0, deadline);
        assertEq(counter.callCount(), 1, "target still called exactly once");
    }

    /// 6. Target failure is retryable per the documented rule: first failure
    ///    stays SCHEDULED, retry allowed after cooldown, and eventually succeeds.
    function test_failureThenRetry_succeedsOnce() public {
        FlakyTarget flaky = new FlakyTarget(); // starts in the "failing" state
        uint64 deadline = _deadline();
        bytes memory data = abi.encodeCall(FlakyTarget.run, ());
        uint64 n = 0;
        bytes32 digest = _digest(address(flaky), 0, keccak256(data), n, deadline);
        bytes[] memory sigs = _sigArray(_sign(digest, PK0), _sign(digest, PK1));
        bytes32 id = executor.propose(address(flaky), 0, data, n, deadline, sigs);

        vm.warp(block.timestamp + TIMELOCK);
        // Attempt 1: target reverts. Executor must NOT revert - it records failure.
        executor.execute(address(flaky), 0, data, n, deadline);
        (, , , , , , MultisigTimelock.State st1, , uint64 lastAttempt, uint16 fails, , , ) = executor
            .getOp(id);
        assertEq(uint8(st1), uint8(MultisigTimelock.State.Scheduled));
        assertEq(fails, 1, "executor recorded one failure");

        // Retrying immediately is blocked by the cooldown (readyAt=4, now=3).
        vm.expectRevert(
            abi.encodeWithSelector(
                MultisigTimelock.RetryCooldownActive.selector,
                uint256(lastAttempt) + RETRY_COOLDOWN,
                block.timestamp
            )
        );
        executor.execute(address(flaky), 0, data, n, deadline);

        // The transient failure is repaired, then the cooldown elapses.
        flaky.setFailing(false);
        vm.warp(uint256(lastAttempt) + RETRY_COOLDOWN);
        executor.execute(address(flaky), 0, data, n, deadline);
        assertEq(uint8(_state(id)), uint8(MultisigTimelock.State.Executed));
        assertEq(flaky.successes(), 1);
        assertEq(flaky.attempts(), 1, "reverted attempt rolled back target state");

        vm.expectRevert(
            abi.encodeWithSelector(
                MultisigTimelock.AlreadyTerminal.selector,
                id,
                MultisigTimelock.State.Executed
            )
        );
        executor.execute(address(flaky), 0, data, n, deadline);
        assertEq(flaky.successes(), 1, "no second success");
    }

    /// 7. A target that never succeeds exhausts retries -> permanently Failed.
    function test_retriesExhausted_becomesFailed() public {
        AlwaysFail alwaysFail = new AlwaysFail();
        uint64 deadline = _deadline();
        bytes memory data = abi.encodeCall(AlwaysFail.boom, ());
        uint64 n = executor.nonce();
        bytes32 digest = _digest(address(alwaysFail), 0, keccak256(data), n, deadline);
        bytes[] memory sigs = _sigArray(_sign(digest, PK0), _sign(digest, PK1));
        bytes32 id = executor.propose(address(alwaysFail), 0, data, n, deadline, sigs);

        vm.warp(block.timestamp + TIMELOCK);
        executor.execute(address(alwaysFail), 0, data, n, deadline);
        assertEq(uint8(_state(id)), uint8(MultisigTimelock.State.Scheduled));

        vm.warp(block.timestamp + RETRY_COOLDOWN);
        executor.execute(address(alwaysFail), 0, data, n, deadline);
        assertEq(uint8(_state(id)), uint8(MultisigTimelock.State.Failed), "terminal failure");

        vm.expectRevert(
            abi.encodeWithSelector(
                MultisigTimelock.AlreadyTerminal.selector,
                id,
                MultisigTimelock.State.Failed
            )
        );
        executor.execute(address(alwaysFail), 0, data, n, deadline);
    }

    /// 8. Executing after the deadline voids the op instead of running it.
    function test_executeAfterDeadline_voids() public {
        (bytes32 id, bytes memory data, uint64 deadline) = _proposeCounter(
            0,
            1,
            "late",
            PK0, PK1
        );
        vm.warp(uint256(deadline) + 1);
        vm.expectRevert(
            abi.encodeWithSelector(MultisigTimelock.DeadlinePassed.selector, deadline)
        );
        executor.execute(address(counter), 0, data, 0, deadline);
        // The revert cannot persist a Void state change; anyone marks it Void.
        executor.voidExpired(id);
        assertEq(uint8(_state(id)), uint8(MultisigTimelock.State.Void));
        assertEq(counter.callCount(), 0);
    }

    /// 8b. voidExpired marks a stale op Void.
    function test_voidExpired_marksVoid() public {
        (bytes32 id, , uint64 deadline) = _proposeCounter(
            0,
            1,
            "stale",
            PK0, PK1
        );
        vm.warp(uint256(deadline) + 1);
        executor.voidExpired(id);
        assertEq(uint8(_state(id)), uint8(MultisigTimelock.State.Void));
    }

    /// 9. Nonces must be used strictly in order.
    function test_nonceMustBeSequential() public {
        uint64 deadline = _deadline();
        bytes memory data = _counterData(1, "gap");
        bytes32 digest = _digest(address(counter), 0, keccak256(data), 5, deadline);
        bytes[] memory sigs = _sigArray(_sign(digest, PK0), _sign(digest, PK1));
        vm.expectRevert(abi.encodeWithSelector(MultisigTimelock.NonceUsed.selector, 5));
        executor.propose(address(counter), 0, data, 5, deadline, sigs);
    }

    /// 10. Changing signers voids in-flight ops and requires the CURRENT
    ///     threshold; afterwards new signers govern and old signatures are dead.
    function test_changeSigners_resetsInFlightAndOldSignaturesFail() public {
        // Schedule op #0 under the old set.
        (bytes32 oldId, bytes memory oldData, uint64 oldDeadline) = _proposeCounter(
            0,
            1,
            "before-change",
            PK0, PK1
        );
        assertEq(uint8(_state(oldId)), uint8(MultisigTimelock.State.Scheduled));

        // Governance op #1: new set {s1, s2, s3}, threshold 3.
        address[] memory newSigners = new address[](3);
        newSigners[0] = s1;
        newSigners[1] = s2;
        newSigners[2] = s3;
        uint64 govDeadline = _deadline();
        bytes32 govDigest = executor.digestForSetSigners(newSigners, 3, 1, govDeadline);
        bytes[] memory govSigs = _sigArray(
            _sign(govDigest, PK0),
            _sign(govDigest, PK1)
        );
        executor.setSigners(newSigners, 3, 1, govDeadline, govSigs);
        assertEq(executor.configVersion(), 1);
        assertEq(executor.threshold(), 3);
        assertTrue(executor.isSigner(s3));
        assertFalse(executor.isSigner(s0));
        assertEq(executor.nonce(), 2, "governance op claimed nonce 1");

        // The previously scheduled op is permanently invalidated and cannot run.
        assertEq(uint8(_state(oldId)), uint8(MultisigTimelock.State.Invalidated));
        vm.warp(block.timestamp + TIMELOCK);
        vm.expectRevert(
            abi.encodeWithSelector(
                MultisigTimelock.AlreadyTerminal.selector,
                oldId,
                MultisigTimelock.State.Invalidated
            )
        );
        executor.execute(address(counter), 0, oldData, 0, oldDeadline);
        assertEq(counter.callCount(), 0, "invalidated op never executed");

        // A signature produced by the removed signer for a FRESH op is rejected.
        uint64 deadline2 = _deadline();
        bytes memory data2 = _counterData(2, "after-change");
        bytes32 d2 = _digest(address(counter), 0, keccak256(data2), 2, deadline2);
        bytes[] memory stale = _sigArray(_sign(d2, PK0), _sign(d2, PK1), _sign(d2, PK2));
        vm.expectRevert(abi.encodeWithSelector(MultisigTimelock.BadSignature.selector, 0));
        executor.propose(address(counter), 0, data2, 2, deadline2, stale);

        // But 3 valid signatures from the NEW set schedule and execute it.
        bytes[] memory fresh = _sigArray(_sign(d2, PK1), _sign(d2, PK2), _sign(d2, PK3));
        bytes32 id2 = executor.propose(address(counter), 0, data2, 2, deadline2, fresh);
        assertEq(uint8(_state(id2)), uint8(MultisigTimelock.State.Scheduled));
        vm.warp(block.timestamp + TIMELOCK);
        executor.execute(address(counter), 0, data2, 2, deadline2);
        assertEq(counter.count(), 2);
    }

    /// 11. setSigners with too few signatures reverts.
    function test_changeSigners_belowThreshold_reverts() public {
        address[] memory newSigners = new address[](3);
        newSigners[0] = s1;
        newSigners[1] = s2;
        newSigners[2] = s3;
        uint64 deadline = _deadline();
        bytes32 digest = executor.digestForSetSigners(newSigners, 2, 0, deadline);
        bytes[] memory one = new bytes[](1);
        one[0] = _sign(digest, PK0);
        vm.expectRevert(
            abi.encodeWithSelector(MultisigTimelock.ThresholdNotMet.selector, 1, 2)
        );
        executor.setSigners(newSigners, 2, 0, deadline, one);
    }

    /// 12. The operation hash binds target AND parameters: tampering with either
    ///     resolves to a different op id, which is unknown and never executes.
    function test_executionBindsTargetAndData() public {
        (, bytes memory data, uint64 deadline) = _proposeCounter(
            0,
            3,
            "bound",
            PK0, PK1
        );
        vm.warp(block.timestamp + TIMELOCK);

        // Different target, same nonce -> unknown op.
        Counter other = new Counter();
        vm.expectRevert();
        executor.execute(address(other), 0, data, 0, deadline);

        // Same target but mutated calldata -> different dataHash -> unknown op.
        bytes memory tampered = _counterData(999, "bound");
        vm.expectRevert();
        executor.execute(address(counter), 0, tampered, 0, deadline);
        assertEq(counter.callCount(), 0);

        // Untampered execution still works.
        executor.execute(address(counter), 0, data, 0, deadline);
        assertEq(counter.count(), 3);
    }

    /// 13. Re-entrant target cannot execute the op a second time mid-call.
    function test_reentrancyBlocked_singleSuccess() public {
        ReentrantTarget rt = new ReentrantTarget(address(executor));
        uint64 deadline = _deadline();
        bytes memory data = abi.encodeCall(ReentrantTarget.callback, (uint64(0), deadline));
        uint64 n = executor.nonce();
        bytes32 digest = _digest(address(rt), 0, keccak256(data), n, deadline);
        bytes[] memory sigs = _sigArray(_sign(digest, PK0), _sign(digest, PK1));
        bytes32 id = executor.propose(address(rt), 0, data, n, deadline, sigs);

        vm.warp(block.timestamp + TIMELOCK);
        executor.execute(address(rt), 0, data, n, deadline);
        assertEq(uint8(_state(id)), uint8(MultisigTimelock.State.Executed));
        assertEq(rt.successes(), 1, "reentrant call did not double-execute");
    }

    /// 14. High-s (malleable) signatures are rejected.
    function test_highSSignature_reverts() public {
        uint64 deadline = _deadline();
        bytes memory data = _counterData(1, "highs");
        uint64 n = executor.nonce();
        bytes32 digest = _digest(address(counter), 0, keccak256(data), n, deadline);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(PK0, digest);
        // Flip to the high-s / non-canonical twin.
        bytes32 sHigh = bytes32(
            uint256(0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141) -
                uint256(s)
        );
        uint8 vTwin = v == 27 ? 28 : 27;
        bytes[] memory sigs = new bytes[](1);
        sigs[0] = abi.encodePacked(r, sHigh, vTwin);
        vm.expectRevert(abi.encodeWithSelector(MultisigTimelock.BadSignature.selector, 0));
        executor.propose(address(counter), 0, data, n, deadline, sigs);
    }

    function _state(bytes32 id) internal view returns (MultisigTimelock.State) {
        (, , , , , , MultisigTimelock.State state, , , , , , ) = executor.getOp(id);
        return state;
    }
}
