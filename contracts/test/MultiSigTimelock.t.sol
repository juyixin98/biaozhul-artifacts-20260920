// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Vm} from "./Vm.sol";
import {Assert} from "./Assert.sol";
import {MultiSigTimelock} from "../src/MultiSigTimelock.sol";
import {Counter} from "../src/Counter.sol";

/// @dev 链上合约测试：签名乱序、重复去重、延时、失败重试、
///      至多执行一次、过期、nonce 防重放、签名人变更（多签自调用）。
///      不依赖 forge-std，仅使用 forge 内置作弊码 + 自定义断言。
contract MultiSigTimelockTest is Assert {
    Vm constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);

    MultiSigTimelock wallet;
    Counter counter;
    bytes32 domainSeparator;

    // 3 个签名人，阈值 2，延时 1 小时，失败冷却 10 分钟
    uint256 constant PK1 = 0x1111111111111111111111111111111111111111111111111111111111111111;
    uint256 constant PK2 = 0x2222222222222222222222222222222222222222222222222222222222222222;
    uint256 constant PK3 = 0x3333333333333333333333333333333333333333333333333333333333333333;
    uint256 constant PK4 = 0x4444444444444444444444444444444444444444444444444444444444444444;
    uint256 constant PK_ATTACKER = 0x9999;
    address s1;
    address s2;
    address s3;
    address s4;

    uint64 constant DELAY = 3600;
    uint64 constant COOLDOWN = 600;

    event Approval(bytes32 indexed opHash, address indexed signer, uint16 count);
    event Scheduled(bytes32 indexed opHash, uint64 readyAt);
    event Executed(bytes32 indexed opHash, bytes returnData);
    event ExecutionFailed(bytes32 indexed opHash, bytes returnData, uint64 nextTryAfter);
    event SignerConfigured(address[] signers, uint16 threshold);
    event Incremented(uint256 newCount, address caller);

    function setUp() public {
        s1 = vm.addr(PK1);
        s2 = vm.addr(PK2);
        s3 = vm.addr(PK3);
        s4 = vm.addr(PK4);
        address[] memory signers = new address[](3);
        signers[0] = s1;
        signers[1] = s2;
        signers[2] = s3;
        wallet = new MultiSigTimelock(signers, 2, DELAY, COOLDOWN);
        counter = new Counter(address(wallet), 0);
        domainSeparator = wallet.domainSeparator();    }

    // ------------------------------------------------------------------
    // 辅助
    // ------------------------------------------------------------------

    function _digest(
        address target,
        bytes memory data,
        uint256 nonce,
        uint64 validUntil
    ) internal view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(
                wallet.OPERATION_TYPEHASH(),
                target,
                keccak256(data),
                uint256(0),
                nonce,
                validUntil
            )
        );
        return keccak256(abi.encodePacked("\x19\x01", domainSeparator, structHash));
    }

    function _sign(uint256 pk, bytes32 digest) internal returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 sv) = vm.sign(pk, digest);
        return abi.encodePacked(r, sv, v);
    }

    function _sigs(uint256 a, uint256 b, bytes32 digest) internal returns (bytes[] memory out) {
        out = new bytes[](2);
        out[0] = _sign(a, digest);
        out[1] = _sign(b, digest);
    }

    function _incData() internal pure returns (bytes memory) {
        return abi.encodeCall(Counter.increment, ());
    }

    // ------------------------------------------------------------------
    // 1. 乱序签名：s3 先签、s1 后签，仍达到阈值并调度
    // ------------------------------------------------------------------

    function test_01_OutOfOrderSignaturesSchedules() public {
        bytes memory data = _incData();
        uint256 nonce = 1;
        bytes32 digest = _digest(address(counter), data, nonce, 0);
        bytes32 opHash = wallet.hashOperation(address(counter), data, 0, nonce, 0);
        assertEq(digest, opHash, "offchain digest must match onchain hash");

        bytes[] memory sigs = new bytes[](2);
        sigs[0] = _sign(PK3, digest); // 乱序：3 号先签
        sigs[1] = _sign(PK1, digest); // 1 号后签

        vm.expectEmit(true, false, false, true, address(wallet));
        emit Scheduled(opHash, uint64(block.timestamp) + DELAY);
        (bytes32 h, uint16 count) = wallet.approve(address(counter), data, 0, nonce, 0, sigs);
        assertEq(h, opHash, "op hash mismatch");
        assertEq(uint256(count), 2, "approval count should be 2");

        MultiSigTimelock.Operation memory op = wallet.getOperation(opHash);
        assertTrue(op.scheduled, "should be scheduled");
        assertEq(uint256(op.readyAt), block.timestamp + DELAY, "readyAt = now + delay");
    }

    // ------------------------------------------------------------------
    // 2. 重复签名去重
    // ------------------------------------------------------------------

    function test_02_DuplicateSignaturesDeduplicated() public {
        bytes memory data = _incData();
        uint256 nonce = 2;
        bytes32 digest = _digest(address(counter), data, nonce, 0);

        bytes[] memory sigs = new bytes[](4);
        sigs[0] = _sign(PK1, digest);
        sigs[1] = _sign(PK1, digest); // 完全重复
        sigs[2] = _sign(PK1, digest);
        sigs[3] = _sign(PK1, digest);

        (, uint16 count) = wallet.approve(address(counter), data, 0, nonce, 0, sigs);
        assertEq(uint256(count), 1, "same signer in one batch must count once");

        bytes[] memory sigs2 = new bytes[](1);
        sigs2[0] = _sign(PK1, digest);
        (, count) = wallet.approve(address(counter), data, 0, nonce, 0, sigs2);
        assertEq(uint256(count), 1, "same signer across txs must count once");
        assertFalse(wallet.nonceUsed(nonce), "nonce not consumed until threshold");

        bytes[] memory bad = new bytes[](1);
        bad[0] = _sign(PK_ATTACKER, digest);
        vm.expectRevert(MultiSigTimelock.InvalidSignature.selector);
        wallet.approve(address(counter), data, 0, nonce, 0, bad);
    }

    // 阈值不足时分两笔交易凑齐签名也能调度（乱序/异步收集）
    function test_02b_ApprovalsAcrossTransactions() public {
        bytes memory data = _incData();
        uint256 nonce = 20;
        bytes32 digest = _digest(address(counter), data, nonce, 0);

        bytes[] memory one = new bytes[](1);
        one[0] = _sign(PK3, digest);
        (, uint16 count) = wallet.approve(address(counter), data, 0, nonce, 0, one);
        assertEq(uint256(count), 1, "first approval");

        bytes[] memory two = new bytes[](1);
        two[0] = _sign(PK1, digest);
        (, count) = wallet.approve(address(counter), data, 0, nonce, 0, two);
        assertEq(uint256(count), 2, "second approval reaches threshold");
        assertTrue(wallet.nonceUsed(nonce), "nonce consumed at scheduling");
    }

    // ------------------------------------------------------------------
    // 3. 延时未到不能执行；到点执行成功，且只执行一次
    // ------------------------------------------------------------------

    function test_03_TimelockDelayThenExecuteOnce() public {
        bytes memory data = _incData();
        uint256 nonce = 3;
        bytes32 digest = _digest(address(counter), data, nonce, 0);
        bytes32 opHash = wallet.hashOperation(address(counter), data, 0, nonce, 0);

        bytes[] memory sigs = _sigs(PK1, PK2, digest);
        wallet.approve(address(counter), data, 0, nonce, 0, sigs);

        vm.expectRevert(
            abi.encodeWithSelector(
                MultiSigTimelock.TimelockNotReady.selector,
                uint64(block.timestamp) + DELAY
            )
        );
        wallet.execute(address(counter), data, 0, nonce, 0);
        assertEq(counter.count(), 0, "must not execute before delay");

        bytes memory dataUnapproved = _incData();
        vm.expectRevert(MultiSigTimelock.NotScheduled.selector);
        wallet.execute(address(counter), dataUnapproved, 0, nonce + 999, 0);

        vm.warp(block.timestamp + DELAY);
        vm.expectEmit(true, false, false, false, address(counter));
        emit Incremented(1, address(wallet));
        wallet.execute(address(counter), data, 0, nonce, 0);
        assertEq(counter.count(), 1, "target called once");

        vm.expectRevert(MultiSigTimelock.AlreadyExecuted.selector);
        wallet.execute(address(counter), data, 0, nonce, 0);
        assertEq(counter.count(), 1, "target must not be called twice");
        assertTrue(wallet.getOperation(opHash).executed, "executed flag set");
    }

    // ------------------------------------------------------------------
    // 4. 目标先失败：冷却期内禁止重试，冷却后重试成功；成功后不可再执行
    // ------------------------------------------------------------------

    function test_04_TargetFailureRetryThenSuccessOnce() public {
        // 在 4200 之前调用失败：readyAt=3601 时第一次执行失败，
        // 冷却到 4201 后目标恰好“恢复”，重试成功。
        Counter flaky = new Counter(address(wallet), uint64(DELAY + COOLDOWN));
        bytes memory data = _incData();
        uint256 nonce = 4;
        bytes32 digest = _digest(address(flaky), data, nonce, 0);
        bytes32 opHash = wallet.hashOperation(address(flaky), data, 0, nonce, 0);

        bytes[] memory sigs = _sigs(PK2, PK3, digest);
        wallet.approve(address(flaky), data, 0, nonce, 0, sigs);
        vm.warp(block.timestamp + DELAY);

        // 第一次执行：目标 revert，返回标准 Error(string) 数据；不置 executed
        bytes memory errData = abi.encodeWithSignature("Error(string)", "flaky target: failing on purpose");
        vm.expectEmit(true, false, false, true, address(wallet));
        emit ExecutionFailed(opHash, errData, uint64(block.timestamp) + COOLDOWN);
        wallet.execute(address(flaky), data, 0, nonce, 0);
        assertFalse(wallet.getOperation(opHash).executed, "failure is not terminal");
        assertEq(flaky.count(), 0, "no success yet");

        // 冷却期内重试 -> 拒绝（retryCooldown 规则）
        uint64 nextTry = uint64(block.timestamp) + COOLDOWN;
        vm.warp(block.timestamp + COOLDOWN - 1);
        vm.expectRevert(
            abi.encodeWithSelector(MultiSigTimelock.RetryCooldownActive.selector, nextTry)
        );
        wallet.execute(address(flaky), data, 0, nonce, 0);
        assertEq(flaky.count(), 0, "no retry during cooldown");

        // 冷却过后且目标已恢复 -> 重试成功
        vm.warp(block.timestamp + 1);
        vm.expectEmit(true, false, false, false, address(flaky));
        emit Incremented(1, address(wallet));
        wallet.execute(address(flaky), data, 0, nonce, 0);
        assertEq(flaky.count(), 1, "retry succeeds after cooldown");

        // 成功后再执行 -> AlreadyExecuted，目标至多成功执行一次
        vm.warp(block.timestamp + COOLDOWN);
        vm.expectRevert(MultiSigTimelock.AlreadyExecuted.selector);
        wallet.execute(address(flaky), data, 0, nonce, 0);
        assertEq(flaky.count(), 1, "target must not succeed twice");
    }

    // 持续失败时可按 cooldown 反复重试，始终不置 executed、不产生成功调用
    function test_04b_RepeatedFailuresKeepRetriable() public {
        Counter flaky = new Counter(address(wallet), type(uint64).max); // 永远失败
        bytes memory data = _incData();
        uint256 nonce = 21;
        bytes32 digest = _digest(address(flaky), data, nonce, 0);
        bytes[] memory sigs = _sigs(PK1, PK2, digest);
        wallet.approve(address(flaky), data, 0, nonce, 0, sigs);
        vm.warp(block.timestamp + DELAY);

        bytes32 opHash = wallet.hashOperation(address(flaky), data, 0, nonce, 0);
        for (uint256 i = 1; i <= 3; i++) {
            wallet.execute(address(flaky), data, 0, nonce, 0);
            // 每次失败都推进 lastFailureAt
            assertEq(
                uint256(wallet.getOperation(opHash).lastFailureAt),
                block.timestamp,
                "failure timestamp advances per attempt"
            );
            assertFalse(wallet.getOperation(opHash).executed, "still retriable");
            vm.warp(block.timestamp + COOLDOWN);
        }
        assertEq(flaky.count(), 0, "never succeeded");
    }

    // ------------------------------------------------------------------
    // 5. validUntil 时限
    // ------------------------------------------------------------------

    function test_05_ExpiredOperationRejectedAtApprove() public {
        bytes memory data = _incData();
        uint256 nonce = 5;
        uint64 validUntil = 1000;
        bytes32 digest = _digest(address(counter), data, nonce, validUntil);

        bytes[] memory sigs = _sigs(PK1, PK2, digest);
        vm.warp(1001);
        vm.expectRevert(MultiSigTimelock.OperationExpired.selector);
        wallet.approve(address(counter), data, 0, nonce, validUntil, sigs);
    }

    function test_05b_ExpiresAfterScheduledBeforeExecution() public {
        bytes memory data = _incData();
        uint256 nonce = 6;
        uint64 validUntil = uint64(DELAY + 100);
        bytes32 digest = _digest(address(counter), data, nonce, validUntil);

        bytes[] memory sigs = _sigs(PK1, PK2, digest);
        wallet.approve(address(counter), data, 0, nonce, validUntil, sigs);

        vm.warp(validUntil + 1);
        vm.expectRevert(MultiSigTimelock.OperationExpired.selector);
        wallet.execute(address(counter), data, 0, nonce, validUntil);
    }

    // ------------------------------------------------------------------
    // 6. nonce 防重放
    // ------------------------------------------------------------------

    function test_06_NonceConsumedAndReplayRejected() public {
        bytes memory data = _incData();
        uint256 nonce = 7;
        bytes32 digest = _digest(address(counter), data, nonce, 0);

        bytes[] memory sigs = _sigs(PK1, PK2, digest);
        wallet.approve(address(counter), data, 0, nonce, 0, sigs);
        assertTrue(wallet.nonceUsed(nonce), "nonce consumed");

        // 同 nonce 换目标也必须拒绝
        Counter other = new Counter(address(wallet), 0);
        bytes memory dataO = _incData();
        bytes32 digestO = _digest(address(other), dataO, nonce, 0);
        bytes[] memory sigsO = _sigs(PK1, PK2, digestO);
        vm.expectRevert(MultiSigTimelock.NonceAlreadyUsed.selector);
        wallet.approve(address(other), dataO, 0, nonce, 0, sigsO);
    }

    // ------------------------------------------------------------------
    // 7. 签名人变更：只有多签自调用 configureSigners 才能改
    // ------------------------------------------------------------------

    function test_07_SignerChangeViaSelfCall() public {
        address[] memory newSigners = new address[](3);
        newSigners[0] = s1;
        newSigners[1] = s2;
        newSigners[2] = s3;
        vm.expectRevert(MultiSigTimelock.Unauthorized.selector);
        wallet.configureSigners(newSigners, 2);

        // 多签提案：移除 s3，新增 s4
        newSigners[2] = s4;
        bytes memory data = abi.encodeCall(wallet.configureSigners, (newSigners, 2));
        uint256 nonce = 8;
        bytes32 digest = _digest(address(wallet), data, nonce, 0);
        bytes32 opHash = wallet.hashOperation(address(wallet), data, 0, nonce, 0);

        bytes[] memory sigs = _sigs(PK2, PK3, digest);
        wallet.approve(address(wallet), data, 0, nonce, 0, sigs);
        vm.warp(block.timestamp + DELAY);
        // SignerConfigured 在 execute 触发自调用时才发出
        vm.expectEmit(false, false, false, true, address(wallet));
        emit SignerConfigured(newSigners, 2);
        wallet.execute(address(wallet), data, 0, nonce, 0);

        assertFalse(wallet.isSigner(s3), "s3 removed");
        assertTrue(wallet.isSigner(s4), "s4 added");
        assertEq(uint256(wallet.threshold()), 2, "threshold stays 2");
        assertTrue(wallet.getOperation(opHash).executed, "change op executed");

        // 变更后旧签名人 s3 的签名无效
        bytes memory incData = _incData();
        uint256 nonce2 = 9;
        bytes32 d2 = _digest(address(counter), incData, nonce2, 0);
        bytes[] memory staleSigs = _sigs(PK3, PK1, d2);
        vm.expectRevert(MultiSigTimelock.InvalidSignature.selector);
        wallet.approve(address(counter), incData, 0, nonce2, 0, staleSigs);

        bytes[] memory freshSigs = _sigs(PK1, PK4, d2);
        (, uint16 count) = wallet.approve(address(counter), incData, 0, nonce2, 0, freshSigs);
        assertEq(uint256(count), 2, "new signer set governs new ops");
    }

    // ------------------------------------------------------------------
    // 8. 执行回调返回数据
    // ------------------------------------------------------------------

    function test_08_ExecuteReturnData() public {
        bytes memory data = abi.encodeCall(Counter.echo, (uint256(42)));
        uint256 nonce = 10;
        bytes32 digest = _digest(address(counter), data, nonce, 0);
        bytes[] memory sigs = _sigs(PK1, PK3, digest);
        wallet.approve(address(counter), data, 0, nonce, 0, sigs);
        vm.warp(block.timestamp + DELAY);

        bytes memory ret = wallet.execute(address(counter), data, 0, nonce, 0);
        assertEq(abi.decode(ret, (uint256)), 42, "callback return data echoes 42");
    }

    // ------------------------------------------------------------------
    // 9. 构造参数校验
    // ------------------------------------------------------------------

    function test_09_ConstructorRejectsBadConfig() public {
        address[] memory one = new address[](1);
        one[0] = s1;
        vm.expectRevert(MultiSigTimelock.InvalidThreshold.selector);
        new MultiSigTimelock(one, 2, DELAY, COOLDOWN);

        address[] memory empty = new address[](0);
        vm.expectRevert(MultiSigTimelock.EmptySigners.selector);
        new MultiSigTimelock(empty, 1, DELAY, COOLDOWN);

        address[] memory dup = new address[](2);
        dup[0] = s1;
        dup[1] = s1;
        vm.expectRevert(MultiSigTimelock.DuplicateSigner.selector);
        new MultiSigTimelock(dup, 1, DELAY, COOLDOWN);
    }
}
