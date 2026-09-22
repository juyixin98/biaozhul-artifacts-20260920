// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test, Vm} from "forge-std/Test.sol";
import {VestingStream} from "../src/VestingStream.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

/// @notice 随机操作序列 Handler：对归属合约执行 fuzz 驱动的
///         createStream / topUp / claim / cancel / warp 及同时间重复调用，
///         同时维护一个独立的状态模型与幽灵变量，并逐步核对合约发出的事件。
contract VestingHandler is Test {
    uint256 public constant INITIAL_BALANCE = 1e30;
    uint256 public constant MAX_STREAMS = 24;

    VestingStream public immutable vesting;
    MockERC20 public immutable token;

    address[] public senders;
    address[] public beneficiaries;

    // ---------------- 独立模型 ----------------
    struct MStream {
        address sender;
        address beneficiary;
        uint128 amount;
        uint128 claimed;
        uint128 refunded;
        uint64 start;
        uint64 cliff;
        uint64 end;
        uint64 canceledAt;
    }

    MStream[] public streams;

    // ---------------- 幽灵变量 ----------------
    uint256 public ghost_deposited; // 累计存入（创建 + 补足）
    uint256 public ghost_claimed; // 累计领取
    uint256 public ghost_refunded; // 累计撤销退款
    mapping(address => uint256) public ghost_minted;
    mapping(address => uint256) public ghost_out; // 各 actor 流出（存入合约）
    mapping(address => uint256) public ghost_in; // 各 actor 流入（领取 + 退款）

    bytes32 constant STREAM_CREATED_SIG =
        keccak256("StreamCreated(uint256,address,address,uint256,uint64,uint64,uint64)");
    bytes32 constant TOPPED_UP_SIG = keccak256("ToppedUp(uint256,uint256,uint256)");
    bytes32 constant CLAIMED_SIG = keccak256("Claimed(uint256,uint256)");
    bytes32 constant CANCELED_SIG = keccak256("Canceled(uint256,uint256,uint256)");

    constructor(VestingStream _vesting, MockERC20 _token) {
        vesting = _vesting;
        token = _token;
        for (uint256 i = 0; i < 3; i++) {
            address s = address(uint160(0x1000 + i));
            address b = address(uint160(0x2000 + i));
            senders.push(s);
            beneficiaries.push(b);
            token.mint(s, INITIAL_BALANCE);
            ghost_minted[s] = INITIAL_BALANCE;
            vm.prank(s);
            token.approve(address(_vesting), type(uint256).max);
        }
    }

    function streamCount() external view returns (uint256) {
        return streams.length;
    }

    // ---------------- 模型计算（与合约实现相互独立） ----------------

    function modelVested(uint256 i, uint64 t) public view returns (uint256) {
        MStream memory s = streams[i];
        uint64 eff = s.canceledAt != 0 ? s.canceledAt : t;
        if (eff < s.cliff) return 0;
        if (eff >= s.end) return s.amount;
        // 此处必有 start <= cliff <= eff < end，减法安全
        return uint256(s.amount) * (eff - s.start) / (s.end - s.start);
    }

    function modelClaimable(uint256 i) public view returns (uint256) {
        return modelVested(i, uint64(block.timestamp)) - streams[i].claimed;
    }

    // ---------------- 事件核对工具 ----------------

    /// @dev 过滤出归属合约发出的事件（代币的 Transfer 事件会被排除）。
    function _vestingLogs() internal returns (Vm.Log[] memory) {
        Vm.Log[] memory logs = vm.getRecordedLogs();
        uint256 n;
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter == address(vesting)) n++;
        }
        Vm.Log[] memory out = new Vm.Log[](n);
        uint256 j;
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter == address(vesting)) out[j++] = logs[i];
        }
        return out;
    }

    function _checkCreatedData(
        bytes memory data,
        address sender,
        address beneficiary,
        uint256 amount,
        uint64 start,
        uint64 cliff,
        uint64 end
    ) internal {
        (address evSender, address evBeneficiary, uint256 evAmount, uint64 evStart, uint64 evCliff, uint64 evEnd)
        = abi.decode(data, (address, address, uint256, uint64, uint64, uint64));
        assertEq(evSender, sender);
        assertEq(evBeneficiary, beneficiary);
        assertEq(evAmount, amount);
        assertEq(evStart, start);
        assertEq(evCliff, cliff);
        assertEq(evEnd, end);
    }

    // ---------------- 随机操作 ----------------

    function createStream(
        uint256 senderSeed,
        uint256 beneficiarySeed,
        uint128 amount,
        uint64 startDelta,
        uint64 duration,
        uint64 cliffDelta
    ) external {
        if (streams.length >= MAX_STREAMS) return;
        amount = uint128(bound(amount, 1, 1e24));
        duration = uint64(bound(duration, 1, 1_000_000));
        startDelta = uint64(bound(startDelta, 0, 10_000));
        cliffDelta = uint64(bound(cliffDelta, 0, duration));

        address sender = senders[senderSeed % senders.length];
        address beneficiary = beneficiaries[beneficiarySeed % beneficiaries.length];
        uint64 start = uint64(block.timestamp) + startDelta;
        uint64 cliff = start + cliffDelta;
        uint64 end = start + duration;
        uint256 id = streams.length;

        vm.recordLogs();
        vm.prank(sender);
        uint256 gotId = vesting.createStream(beneficiary, amount, start, cliff, end);

        // 事件核对：恰好一条 StreamCreated，字段与输入一致
        Vm.Log[] memory logs = _vestingLogs();
        assertEq(logs.length, 1, unicode"createStream 应发出 1 条事件");
        assertEq(logs[0].topics[0], STREAM_CREATED_SIG);
        assertEq(uint256(logs[0].topics[1]), id);
        _checkCreatedData(logs[0].data, sender, beneficiary, amount, start, cliff, end);
        assertEq(gotId, id);

        streams.push(
            MStream(sender, beneficiary, amount, 0, 0, start, cliff, end, 0)
        );
        ghost_deposited += amount;
        ghost_out[sender] += amount;
    }

    function warp(uint64 dt) external {
        dt = uint64(bound(dt, 0, 2 days));
        vm.warp(block.timestamp + dt);
    }

    function claim(uint256 streamSeed) external {
        uint256 n = streams.length;
        if (n == 0) return;
        uint256 i = streamSeed % n;
        MStream storage s = streams[i];
        uint256 expected = modelClaimable(i);

        if (expected == 0) {
            vm.prank(s.beneficiary);
            try vesting.claim(i) {
                revert(unicode"模型为 0 时 claim 必须 revert");
            } catch (bytes memory reason) {
                assertEq(bytes4(reason), VestingStream.NothingToClaim.selector);
            }
            return;
        }

        vm.recordLogs();
        vm.prank(s.beneficiary);
        vesting.claim(i);

        Vm.Log[] memory logs = _vestingLogs();
        assertEq(logs.length, 1, unicode"claim 应发出 1 条事件");
        assertEq(logs[0].topics[0], CLAIMED_SIG);
        assertEq(uint256(logs[0].topics[1]), i);
        assertEq(abi.decode(logs[0].data, (uint256)), expected, unicode"Claimed 事件金额须等于模型可领额");

        s.claimed += uint128(expected);
        ghost_claimed += expected;
        ghost_in[s.beneficiary] += expected;
    }

    /// @dev 同一时间戳连续领取两次：第二次必须因无可领额度而 revert。
    function claimTwiceSameTimestamp(uint256 streamSeed) external {
        uint256 n = streams.length;
        if (n == 0) return;
        uint256 i = streamSeed % n;
        MStream storage s = streams[i];
        uint256 expected = modelClaimable(i);
        if (expected > 0) {
            vm.prank(s.beneficiary);
            vesting.claim(i);
            s.claimed += uint128(expected);
            ghost_claimed += expected;
            ghost_in[s.beneficiary] += expected;
        }
        vm.prank(s.beneficiary);
        try vesting.claim(i) {
            revert(unicode"同一时间重复 claim 必须 revert");
        } catch (bytes memory reason) {
            assertEq(bytes4(reason), VestingStream.NothingToClaim.selector);
        }
    }

    function cancel(uint256 streamSeed) external {
        uint256 n = streams.length;
        if (n == 0) return;
        uint256 i = streamSeed % n;
        MStream storage s = streams[i];

        if (s.canceledAt != 0) {
            vm.prank(s.sender);
            try vesting.cancel(i) {
                revert(unicode"重复 cancel 必须 revert");
            } catch (bytes memory reason) {
                assertEq(bytes4(reason), VestingStream.StreamCanceled.selector);
            }
            return;
        }

        uint256 vestedNow = modelVested(i, uint64(block.timestamp));
        uint256 unvested = s.amount - vestedNow;
        uint256 vestedRemaining = vestedNow - s.claimed;

        vm.recordLogs();
        vm.prank(s.sender);
        vesting.cancel(i);

        Vm.Log[] memory logs = _vestingLogs();
        assertEq(logs.length, 1, unicode"cancel 应发出 1 条事件");
        assertEq(logs[0].topics[0], CANCELED_SIG);
        assertEq(uint256(logs[0].topics[1]), i);
        (uint256 evRefunded, uint256 evVestedRemaining) = abi.decode(logs[0].data, (uint256, uint256));
        assertEq(evRefunded, unvested, unicode"Canceled 事件退款额须等于模型未归属额");
        assertEq(evVestedRemaining, vestedRemaining, unicode"Canceled 事件保留额须等于模型已归属未领取额");

        s.canceledAt = uint64(block.timestamp);
        s.refunded = uint128(unvested);
        ghost_refunded += unvested;
        ghost_in[s.sender] += unvested;
    }

    /// @dev 同一时间戳连续撤销两次：第二次必须 revert。
    function cancelTwiceSameTimestamp(uint256 streamSeed) external {
        uint256 n = streams.length;
        if (n == 0) return;
        uint256 i = streamSeed % n;
        MStream storage s = streams[i];
        if (s.canceledAt == 0) {
            uint256 vestedNow = modelVested(i, uint64(block.timestamp));
            uint256 unvested = s.amount - vestedNow;
            vm.prank(s.sender);
            vesting.cancel(i);
            s.canceledAt = uint64(block.timestamp);
            s.refunded = uint128(unvested);
            ghost_refunded += unvested;
            ghost_in[s.sender] += unvested;
        }
        vm.prank(s.sender);
        try vesting.cancel(i) {
            revert(unicode"同一时间重复 cancel 必须 revert");
        } catch (bytes memory reason) {
            assertEq(bytes4(reason), VestingStream.StreamCanceled.selector);
        }
    }

    function topUp(uint256 streamSeed, uint128 extra) external {
        uint256 n = streams.length;
        if (n == 0) return;
        uint256 i = streamSeed % n;
        MStream storage s = streams[i];
        extra = uint128(bound(extra, 1, 1e24));

        if (s.canceledAt != 0) {
            vm.prank(s.sender);
            try vesting.topUp(i, extra) {
                revert(unicode"撤销后 topUp 必须 revert");
            } catch (bytes memory reason) {
                assertEq(bytes4(reason), VestingStream.StreamCanceled.selector);
            }
            return;
        }

        // 保证 sender 有足够余额（增发部分计入幽灵变量）
        token.mint(s.sender, extra);
        ghost_minted[s.sender] += extra;

        vm.recordLogs();
        vm.prank(s.sender);
        vesting.topUp(i, extra);

        Vm.Log[] memory logs = _vestingLogs();
        assertEq(logs.length, 1, unicode"topUp 应发出 1 条事件");
        assertEq(logs[0].topics[0], TOPPED_UP_SIG);
        assertEq(uint256(logs[0].topics[1]), i);
        (uint256 evExtra, uint256 evNewTotal) = abi.decode(logs[0].data, (uint256, uint256));
        assertEq(evExtra, extra);
        assertEq(evNewTotal, uint256(s.amount) + extra, unicode"ToppedUp 事件新总额须等于模型总额");

        s.amount += extra;
        ghost_deposited += extra;
        ghost_out[s.sender] += extra;
    }

    // ---------------- 权限负向操作 ----------------

    function claimAsWrongActor(uint256 streamSeed, uint256 actorSeed) external {
        uint256 n = streams.length;
        if (n == 0) return;
        uint256 i = streamSeed % n;
        address actor = senders[actorSeed % senders.length];
        if (actor == streams[i].beneficiary) return;
        vm.prank(actor);
        try vesting.claim(i) {
            revert(unicode"非受益人 claim 必须 revert");
        } catch (bytes memory reason) {
            assertEq(bytes4(reason), VestingStream.NotBeneficiary.selector);
        }
    }

    function cancelAsWrongActor(uint256 streamSeed, uint256 actorSeed) external {
        uint256 n = streams.length;
        if (n == 0) return;
        uint256 i = streamSeed % n;
        address actor = beneficiaries[actorSeed % beneficiaries.length];
        if (actor == streams[i].sender) return;
        vm.prank(actor);
        try vesting.cancel(i) {
            revert(unicode"非发送者 cancel 必须 revert");
        } catch (bytes memory reason) {
            assertEq(bytes4(reason), VestingStream.NotSender.selector);
        }
    }

    function topUpAsWrongActor(uint256 streamSeed, uint256 actorSeed) external {
        uint256 n = streams.length;
        if (n == 0) return;
        uint256 i = streamSeed % n;
        address actor = beneficiaries[actorSeed % beneficiaries.length];
        if (actor == streams[i].sender) return;
        vm.prank(actor);
        try vesting.topUp(i, 1) {
            revert(unicode"非发送者 topUp 必须 revert");
        } catch (bytes memory reason) {
            assertEq(bytes4(reason), VestingStream.NotSender.selector);
        }
    }

    // ---------------- 供不变量读取 ----------------

    function getSenders() external view returns (address[] memory) {
        return senders;
    }

    function getBeneficiaries() external view returns (address[] memory) {
        return beneficiaries;
    }

    function getModel(uint256 i) external view returns (MStream memory) {
        return streams[i];
    }
}
