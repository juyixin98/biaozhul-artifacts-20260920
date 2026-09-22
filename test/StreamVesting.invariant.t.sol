// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test, Vm} from "forge-std/Test.sol";
import {StreamVesting} from "../src/StreamVesting.sol";
import {MockERC20} from "../src/mocks/MockERC20.sol";

/// @notice 不变量测试的 Handler：执行随机操作序列（建流/推进时间/领取/撤销/补足/越权尝试），
///         每一步都用独立模型核对合约的余额、事件与可领取额。
contract VestingHandler is Test {
    StreamVesting public immutable vesting;
    MockERC20 public immutable token;

    address[] public senders;
    address[] public beneficiaries;
    address public immutable stranger = makeAddr("stranger");

    // ---- 幽灵变量：独立账本 ----
    uint256 public ghostDeposited; // 建流 + 补足流入合约的总额
    uint256 public ghostWithdrawn; // 受益人累计领取
    uint256 public ghostRefunded; // 撤销退回发送者累计
    uint256 public ghostUnauthorizedBlocked; // 被拒绝的越权调用次数
    uint256 public ghostSameTimeRepeats; // 同时间重复调用次数

    // ---- 独立模型 ----
    struct Model {
        bool exists;
        bool cancelled;
        address sender;
        address beneficiary;
        uint128 amount; // cancel 后收缩为 vestedAtCancel（与合约一致）
        uint128 withdrawn;
        uint64 start;
        uint64 cliff;
        uint64 end;
    }

    uint256[] public ids;
    mapping(uint256 => Model) public models;

    bytes32 internal constant CREATED = keccak256("StreamCreated(uint256,address,address,uint128,uint64,uint64,uint64)");
    bytes32 internal constant WITHDRAWN = keccak256("Withdrawn(uint256,address,uint128)");
    bytes32 internal constant CANCELLED = keccak256("StreamCancelled(uint256,uint128,uint128)");
    bytes32 internal constant TOPPED_UP = keccak256("StreamToppedUp(uint256,uint128,uint128)");

    constructor(StreamVesting _vesting, MockERC20 _token) {
        vesting = _vesting;
        token = _token;
        for (uint256 i = 0; i < 3; i++) {
            senders.push(makeAddr(string.concat("sender", vm.toString(i))));
            beneficiaries.push(makeAddr(string.concat("beneficiary", vm.toString(i))));
        }
    }

    function streamCount() external view returns (uint256) {
        return ids.length;
    }

    /// @dev 独立归属模型：rate/rem 拆分路径，与合约的单表达式除法实现不同。
    function modelVested(uint256 id, uint64 t) public view returns (uint128) {
        Model storage m = models[id];
        if (m.cancelled) return m.amount;
        if (t < m.cliff) return 0;
        uint64 duration = m.end - m.start;
        uint64 elapsed = t >= m.end ? duration : t - m.start;
        uint256 rate = uint256(m.amount) / duration;
        uint256 rem = uint256(m.amount) % duration;
        return uint128(rate * elapsed + (rem * elapsed) / duration);
    }

    function modelClaimable(uint256 id) external view returns (uint128) {
        Model storage m = models[id];
        return modelVested(id, uint64(block.timestamp)) - m.withdrawn;
    }

    // ---------------------------------------------------------------
    // 随机操作
    // ---------------------------------------------------------------

    function createStream(uint256 seed) external {
        Model memory m;
        m.sender = senders[seed % senders.length];
        m.beneficiary = beneficiaries[(seed / 3) % beneficiaries.length];
        m.amount = uint128(bound(uint256(keccak256(abi.encode(seed, "amt"))), 1, 1e30));
        m.start = uint64(block.timestamp) + uint64(bound(uint256(keccak256(abi.encode(seed, "st"))), 0, 500));
        uint64 duration = uint64(bound(uint256(keccak256(abi.encode(seed, "dur"))), 1, 100_000));
        m.end = m.start + duration;
        m.cliff = m.start + uint64(bound(uint256(keccak256(abi.encode(seed, "clf"))), 0, duration));
        m.exists = true;

        token.mint(m.sender, m.amount);
        vm.startPrank(m.sender);
        token.approve(address(vesting), m.amount);
        vm.recordLogs();
        uint256 id = vesting.createStream(m.beneficiary, m.amount, m.start, m.cliff, m.end);
        vm.stopPrank();

        // 事件核对
        (bool found, bytes memory data, bytes32 tId, bytes32 tSender, bytes32 tBeneficiary) =
            _findVestingEvent(CREATED);
        assertTrue(found, "StreamCreated not emitted");
        assertEq(uint256(tId), ids.length, "event id");
        assertEq(address(uint160(uint256(tSender))), m.sender, "event sender");
        assertEq(address(uint160(uint256(tBeneficiary))), m.beneficiary, "event beneficiary");
        (uint128 eAmt, uint64 eStart, uint64 eCliff, uint64 eEnd) =
            abi.decode(data, (uint128, uint64, uint64, uint64));
        assertEq(eAmt, m.amount, "event amount");
        assertEq(eStart, m.start, "event start");
        assertEq(eCliff, m.cliff, "event cliff");
        assertEq(eEnd, m.end, "event end");

        ids.push(id);
        models[id] = m;
        ghostDeposited += m.amount;
    }

    /// @dev 推进 0..2000 秒；0 秒即“同时间重复调用”场景。
    function warp(uint256 seed) external {
        uint64 delta = uint64(bound(seed, 0, 2000));
        if (delta == 0) ghostSameTimeRepeats++;
        vm.warp(block.timestamp + delta);
    }

    function withdraw(uint256 seed) external {
        if (ids.length == 0) return;
        uint256 id = ids[seed % ids.length];
        Model storage m = models[id];
        uint128 expected = modelVested(id, uint64(block.timestamp)) - m.withdrawn;

        if (expected == 0) {
            // 无可领取额时必须 revert（含同时间重复领取）
            vm.prank(m.beneficiary);
            (bool ok,) = address(vesting).call(abi.encodeCall(StreamVesting.withdraw, (id)));
            assertTrue(!ok, "withdraw with zero claimable must revert");
            return;
        }

        uint256 balBefore = token.balanceOf(m.beneficiary);
        vm.recordLogs();
        vm.prank(m.beneficiary);
        vesting.withdraw(id);

        (bool found, bytes memory data,, bytes32 tBeneficiary,) = _findVestingEvent(WITHDRAWN);
        assertTrue(found, "Withdrawn not emitted");
        assertEq(address(uint160(uint256(tBeneficiary))), m.beneficiary, "event beneficiary");
        assertEq(abi.decode(data, (uint128)), expected, "event amount");

        assertEq(token.balanceOf(m.beneficiary) - balBefore, expected, "beneficiary balance delta");
        m.withdrawn += expected;
        ghostWithdrawn += expected;

        // 同时间重复领取必须失败
        vm.prank(m.beneficiary);
        (bool ok2,) = address(vesting).call(abi.encodeCall(StreamVesting.withdraw, (id)));
        assertTrue(!ok2, "second withdraw at same timestamp must revert");
        ghostSameTimeRepeats++;
    }

    function cancel(uint256 seed) external {
        if (ids.length == 0) return;
        uint256 id = ids[seed % ids.length];
        Model storage m = models[id];

        if (m.cancelled) {
            // 重复撤销必须 revert
            vm.prank(m.sender);
            (bool ok,) = address(vesting).call(abi.encodeCall(StreamVesting.cancel, (id)));
            assertTrue(!ok, "second cancel must revert");
            return;
        }

        uint128 vested = modelVested(id, uint64(block.timestamp));
        uint128 refund = m.amount - vested;
        uint256 senderBefore = token.balanceOf(m.sender);

        vm.recordLogs();
        vm.prank(m.sender);
        vesting.cancel(id);

        (bool found, bytes memory data,,,) = _findVestingEvent(CANCELLED);
        assertTrue(found, "StreamCancelled not emitted");
        (uint128 eRefund, uint128 eVested) = abi.decode(data, (uint128, uint128));
        assertEq(eRefund, refund, "event refund");
        assertEq(eVested, vested, "event vested");

        assertEq(token.balanceOf(m.sender) - senderBefore, refund, "sender refund delta");
        m.cancelled = true;
        m.amount = vested;
        ghostRefunded += refund;
    }

    function topUp(uint256 seed) external {
        if (ids.length == 0) return;
        uint256 id = ids[seed % ids.length];
        Model storage m = models[id];

        if (m.cancelled || block.timestamp >= m.end) {
            // 已撤销/已结束的流补足必须 revert
            uint128 extraLate = uint128(bound(seed, 1, 1e24));
            token.mint(m.sender, extraLate);
            vm.startPrank(m.sender);
            token.approve(address(vesting), extraLate);
            (bool ok,) = address(vesting).call(abi.encodeCall(StreamVesting.topUp, (id, extraLate)));
            vm.stopPrank();
            assertTrue(!ok, "topUp on cancelled/ended stream must revert");
            return;
        }

        uint128 extra = uint128(bound(uint256(keccak256(abi.encode(seed, "top"))), 1, 1e24));
        token.mint(m.sender, extra);
        vm.startPrank(m.sender);
        token.approve(address(vesting), extra);
        vm.recordLogs();
        vesting.topUp(id, extra);
        vm.stopPrank();

        (bool found, bytes memory data,,,) = _findVestingEvent(TOPPED_UP);
        assertTrue(found, "StreamToppedUp not emitted");
        (uint128 eExtra, uint128 eNew) = abi.decode(data, (uint128, uint128));
        assertEq(eExtra, extra, "event extra");
        assertEq(eNew, m.amount + extra, "event newAmount");

        m.amount += extra;
        ghostDeposited += extra;
    }

    /// @dev 越权调用必须被拒绝。
    function unauthorized(uint256 seed) external {
        if (ids.length == 0) return;
        uint256 id = ids[seed % ids.length];

        vm.prank(stranger);
        (bool ok1,) = address(vesting).call(abi.encodeCall(StreamVesting.withdraw, (id)));
        assertTrue(!ok1, "stranger withdraw must revert");

        vm.prank(stranger);
        (bool ok2,) = address(vesting).call(abi.encodeCall(StreamVesting.cancel, (id)));
        assertTrue(!ok2, "stranger cancel must revert");

        ghostUnauthorizedBlocked += 2;
    }

    // ---------------------------------------------------------------
    // 事件工具
    // ---------------------------------------------------------------

    /// @dev 在刚记录的日志中找第一条由归属合约发出、topic0 匹配的事件。
    /// 返回 data 与前三个 indexed topics（t1=topics[1] 等，不足则为 0）。
    function _findVestingEvent(bytes32 topic0)
        internal
        returns (bool found, bytes memory data, bytes32 t1, bytes32 t2, bytes32 t3)
    {
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter == address(vesting) && logs[i].topics[0] == topic0) {
                found = true;
                data = logs[i].data;
                if (logs[i].topics.length > 1) t1 = logs[i].topics[1];
                if (logs[i].topics.length > 2) t2 = logs[i].topics[2];
                if (logs[i].topics.length > 3) t3 = logs[i].topics[3];
                return (found, data, t1, t2, t3);
            }
        }
        return (false, bytes(""), bytes32(0), bytes32(0), bytes32(0));
    }
}

/// @notice 不变量：资产守恒 + 合约视图与独立模型一致。
contract StreamVestingInvariantTest is Test {
    MockERC20 internal token;
    StreamVesting internal vesting;
    VestingHandler internal handler;

    function setUp() public {
        token = new MockERC20("Mock", "MCK");
        vesting = new StreamVesting(token);
        handler = new VestingHandler(vesting, token);
        targetContract(address(handler));
    }

    /// 资产守恒：合约余额 == 总流入 - 总领取 - 总退款 == Σ(每条流 amount - withdrawn)
    function invariant_assetConservation() public view {
        uint256 balance = token.balanceOf(address(vesting));
        VestingHandler h = handler;
        assertEq(
            balance,
            h.ghostDeposited() - h.ghostWithdrawn() - h.ghostRefunded(),
            "balance != deposited - withdrawn - refunded"
        );
        uint256 obligations;
        for (uint256 i = 0; i < h.streamCount(); i++) {
            uint256 id = h.ids(i);
            (, , uint128 amount, uint128 withdrawn, , , , ) = vesting.streams(id);
            obligations += amount - withdrawn;
        }
        assertEq(balance, obligations, "balance != sum of stream obligations");
    }

    /// 合约视图与独立模型一致：可领取额、已归属额逐流核对
    function invariant_modelMatchesContract() public view {
        VestingHandler h = handler;
        for (uint256 i = 0; i < h.streamCount(); i++) {
            uint256 id = h.ids(i);
            assertEq(vesting.withdrawable(id), h.modelClaimable(id), "withdrawable mismatch");
            assertEq(
                vesting.vestedOf(id, uint64(block.timestamp)),
                h.modelVested(id, uint64(block.timestamp)),
                "vested mismatch"
            );
            (, , , uint128 withdrawn, , , , ) = vesting.streams(id);
            (, , , , , uint128 mWithdrawn, , , ) = h.models(id);
            assertEq(withdrawn, mWithdrawn, "withdrawn mismatch");
        }
    }

    function invariant_callSummary() public {
        // 便于 -vvv 观察覆盖情况
        emit log_named_uint("streams", handler.streamCount());
        emit log_named_uint("unauthorizedBlocked", handler.ghostUnauthorizedBlocked());
        emit log_named_uint("sameTimeRepeats", handler.ghostSameTimeRepeats());
    }
}
