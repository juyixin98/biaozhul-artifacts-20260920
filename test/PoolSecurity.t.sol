// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {ConstantProductPool} from "../src/ConstantProductPool.sol";
import {IERC20} from "../src/interfaces/IERC20.sol";
import {TestERC20} from "../src/test/TestERC20.sol";
import {FeeOnTransferToken} from "../src/test/FeeOnTransferToken.sol";
import {OutgoingFeeToken} from "../src/test/OutgoingFeeToken.sol";
import {CallbackToken} from "../src/test/CallbackToken.sol";
import {ReentrancyAttacker, AttackerDeployer} from "../src/test/ReentrancyAttacker.sol";

/// @title PoolSecurityTest
/// @notice Adversarial coverage:
///   - sender-side fee-on-transfer rejected (strict incoming delta)
///   - recipient-side fee-on-transfer rejected (active probe at first
///     deposit AND live recipient-credit measurement on every push, so a
///     fee switched on after seeding is still rejected)
///   - ERC-777-style transfer-callback reentrancy blocked on every entry
///   - failed operations leave no state/balance residue (atomicity)
///   - direct token donations mint no shares; sync absorbs them safely
contract PoolSecurityTest is Test {
    uint256 internal dl;

    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");

    function setUp() public {
        dl = block.timestamp + 1 days;
    }

    function _approveMax(address token, address who, address spender) internal {
        vm.prank(who);
        IERC20(token).approve(spender, type(uint256).max);
    }

    // ==================================================================
    // (A) sender-side fee-on-transfer: pool receives less than nominal
    // ==================================================================
    function test_FOT_IncomingFee_FirstMint_Rejected() public {
        TestERC20 h = new TestERC20("H", "H");
        FeeOnTransferToken f = new FeeOnTransferToken("F", "F", 100);
        ConstantProductPool p = new ConstantProductPool(address(h), address(f));
        h.mint(alice, 2000e18);
        f.mint(alice, 2000e18);
        _approveMax(address(h), alice, address(p));
        _approveMax(address(f), alice, address(p));

        vm.prank(alice);
        vm.expectRevert(
            abi.encodeWithSelector(
                ConstantProductPool.UnexpectedBalanceDelta.selector, address(f), uint256(1000e18), uint256(990e18)
            )
        );
        p.addLiquidity(1000e18, 1000e18, 0, alice, dl);

        assertEq(p.totalSupply(), 0);
        assertEq(h.balanceOf(address(p)), 0);
        assertEq(f.balanceOf(address(p)), 0);
    }

    function test_FOT_IncomingFee_SwapInput_Rejected() public {
        // seed a pool honestly by toggling the fee off, then re-enable
        TestERC20 h = new TestERC20("H", "H");
        FeeOnTransferToken f = new FeeOnTransferToken("F", "F", 100);
        ConstantProductPool p = new ConstantProductPool(address(h), address(f));
        h.mint(alice, 2000e18);
        f.mint(alice, 2000e18);
        _approveMax(address(h), alice, address(p));
        _approveMax(address(f), alice, address(p));
        f.setFeeOn(false);
        vm.prank(alice);
        p.addLiquidity(1000e18, 1000e18, 0, alice, dl);
        f.setFeeOn(true);

        f.mint(bob, 100e18);
        _approveMax(address(f), bob, address(p));
        uint256 r0 = h.balanceOf(address(p));
        vm.prank(bob);
        vm.expectRevert(
            abi.encodeWithSelector(
                ConstantProductPool.UnexpectedBalanceDelta.selector, address(f), uint256(10e18), uint256(99e17)
            )
        );
        p.swapExactInput(address(f), 10e18, 0, bob, dl);
        assertEq(h.balanceOf(address(p)), r0, "reserve untouched");
        (uint112 rr0, uint112 rr1,) = p.getReserves();
        assertEq(h.balanceOf(address(p)), uint256(rr0));
        assertEq(f.balanceOf(address(p)), uint256(rr1));
    }

    // ==================================================================
    // (B) recipient-side fee-on-transfer rejected at FIRST deposit by probe
    // ==================================================================
    function test_FOT_OutgoingFee_RejectedAtFirstMint() public {
        PoolFactory factory = new PoolFactory();
        address futurePool = vm.computeCreateAddress(address(factory), vm.getNonce(address(factory)));
        TestERC20 h = new TestERC20("H", "H");
        OutgoingFeeToken o = new OutgoingFeeToken("O", "O", 100, futurePool);
        h.mint(alice, 2000e18);
        o.mint(alice, 2000e18);

        ConstantProductPool p = factory.deploy(address(h), address(o));
        _approveMax(address(h), alice, address(p));
        _approveMax(address(o), alice, address(p));

        vm.prank(alice);
        vm.expectRevert(
            abi.encodeWithSelector(
                ConstantProductPool.FeeOnTransferDetected.selector, address(o), uint256(1000e18), uint256(990e18)
            )
        );
        p.addLiquidity(1000e18, 1000e18, 0, alice, dl);

        assertEq(p.totalSupply(), 0);
    }

    // ==================================================================
    // (C) recipient-side fee switched on after seeding: live push checks
    // ==================================================================
    function test_FOT_OutgoingFee_LiveSwapOutput_Rejected() public {
        // Use a switchable outgoing fee token: honest while seeding, then
        // set the pool as fee victim before a swap.
        SwitchableOutFeeToken o = new SwitchableOutFeeToken("O", "O", 100);
        TestERC20 h = new TestERC20("H2", "H2");
        ConstantProductPool p = new ConstantProductPool(address(h), address(o));
        o.setPool(address(p));
        o.setCharging(false);

        h.mint(alice, 2_000_000e18);
        o.mint(alice, 2_000_000e18);
        _approveMax(address(h), alice, address(p));
        _approveMax(address(o), alice, address(p));
        vm.prank(alice);
        p.addLiquidity(1000e18, 1000e18, 0, alice, dl);

        o.setCharging(true); // fee switched on after liquidity exists

        h.mint(bob, 100e18);
        _approveMax(address(h), bob, address(p));
        uint256 nominal = p.quoteAmountOut(address(h), 10e18);
        // mirror the token's own accounting: credit = nominal - floor(nominal/100)
        uint256 shorted = nominal - nominal / 100;
        vm.prank(bob);
        vm.expectRevert(
            abi.encodeWithSelector(ConstantProductPool.FeeOnTransferDetected.selector, address(o), nominal, shorted)
        );
        p.swapExactInput(address(h), 10e18, 0, bob, dl);

        (uint112 r0, uint112 r1,) = p.getReserves();
        assertEq(h.balanceOf(address(p)), uint256(r0));
        assertEq(o.balanceOf(address(p)), uint256(r1));
    }

    function test_FOT_OutgoingFee_LiveWithdrawal_Rejected() public {
        SwitchableOutFeeToken o = new SwitchableOutFeeToken("O", "O", 100);
        TestERC20 h = new TestERC20("H3", "H3");
        ConstantProductPool p = new ConstantProductPool(address(h), address(o));
        o.setPool(address(p));
        o.setCharging(false);

        h.mint(alice, 2_000_000e18);
        o.mint(alice, 2_000_000e18);
        _approveMax(address(h), alice, address(p));
        _approveMax(address(o), alice, address(p));
        vm.prank(alice);
        (,, uint256 sh) = p.addLiquidity(1000e18, 1000e18, 0, alice, dl);

        o.setCharging(true);

        uint256 hBefore = h.balanceOf(address(p));
        uint256 oBefore = o.balanceOf(address(p));
        // token1 side of the burn
        uint256 burn = sh / 2;
        (, uint256 out1) = _burnQuote(p, burn);
        vm.prank(alice);
        vm.expectRevert(
            abi.encodeWithSelector(
                ConstantProductPool.FeeOnTransferDetected.selector, address(o), out1, out1 - out1 / 100
            )
        );
        p.removeLiquidity(burn, 0, 0, alice, dl);

        assertEq(p.balanceOf(alice), sh, "shares intact");
        assertEq(h.balanceOf(address(p)), hBefore);
        assertEq(o.balanceOf(address(p)), oBefore);
    }

    function _burnQuote(ConstantProductPool p, uint256 shares) internal view returns (uint256 a0, uint256 a1) {
        (uint112 r0, uint112 r1, uint256 ts) = p.getReserves();
        a0 = (shares * uint256(r0)) / ts;
        a1 = (shares * uint256(r1)) / ts;
    }

    // ==================================================================
    // Reentrancy via transfer callbacks (ERC-777-style)
    // ==================================================================
    struct CbEnv {
        TestERC20 a;
        CallbackToken cb;
        ConstantProductPool p;
        ReentrancyAttacker atk;
    }

    function _setupCallbackPool() internal returns (CbEnv memory e) {
        e.a = new TestERC20("A", "A");
        AttackerDeployer deployer = new AttackerDeployer();
        address predicted = vm.computeCreateAddress(address(deployer), vm.getNonce(address(deployer)));
        e.cb = new CallbackToken("B", "B", predicted);
        e.p = new ConstantProductPool(address(e.a), address(e.cb));
        e.a.mint(predicted, 2000e18);
        e.cb.mint(predicted, 2000e18);
        e.atk = deployer.deploy(e.p, e.a, e.cb);
        assertEq(address(e.atk), predicted);
        e.atk.seedLiquidity(1000e18, 1000e18);
        e.a.mint(address(e.atk), 100e18);
        e.cb.mint(address(e.atk), 100e18);
    }

    function test_Reentrancy_BlockedOnSwapInputCallback() public {
        CbEnv memory e = _setupCallbackPool();
        e.atk.arm(ReentrancyAttacker.Mode.REENTER_SWAP);
        e.atk.attackSwap();
        assertTrue(e.atk.innerRevertedAsExpected());
        assertEq(e.atk.lastRevertSelector(), ConstantProductPool.ReentrancyLocked.selector);
        _assertReserves(e.p, e.a, e.cb);
    }

    function test_Reentrancy_BlockedOnSwapOutputCallback() public {
        CbEnv memory e = _setupCallbackPool();
        e.atk.arm(ReentrancyAttacker.Mode.REENTER_ADD);
        e.atk.attackSwapViaOutput();
        assertTrue(e.atk.innerRevertedAsExpected());
        assertEq(e.atk.lastRevertSelector(), ConstantProductPool.ReentrancyLocked.selector);
        _assertReserves(e.p, e.a, e.cb);
    }

    function test_Reentrancy_BlockedOnRemovalCallback() public {
        CbEnv memory e = _setupCallbackPool();
        e.atk.arm(ReentrancyAttacker.Mode.REENTER_REMOVE);
        e.atk.attackRemove();
        assertTrue(e.atk.innerRevertedAsExpected());
        assertEq(e.atk.lastRevertSelector(), ConstantProductPool.ReentrancyLocked.selector);
        _assertReserves(e.p, e.a, e.cb);
    }

    function _assertReserves(ConstantProductPool p, IERC20 a, IERC20 b) internal view {
        (uint112 r0, uint112 r1,) = p.getReserves();
        assertEq(a.balanceOf(address(p)), uint256(r0));
        assertEq(b.balanceOf(address(p)), uint256(r1));
    }

    // ==================================================================
    // Donations / sync
    // ==================================================================
    function test_Donation_MintsNoShares_AndSyncAbsorbs() public {
        TestERC20 x = new TestERC20("X", "X");
        TestERC20 y = new TestERC20("Y", "Y");
        ConstantProductPool p = new ConstantProductPool(address(x), address(y));
        x.mint(alice, 2_000_000e18);
        y.mint(alice, 2_000_000e18);
        _approveMax(address(x), alice, address(p));
        _approveMax(address(y), alice, address(p));
        vm.prank(alice);
        p.addLiquidity(1000e18, 1000e18, 0, alice, dl);

        uint256 supplyBefore = p.totalSupply();
        vm.prank(alice);
        x.transfer(address(p), 5e18);
        assertEq(p.totalSupply(), supplyBefore, "donation mints no shares");

        // strict reserve match at the end of the call catches the donation
        vm.prank(alice);
        vm.expectRevert(
            abi.encodeWithSelector(
                ConstantProductPool.ReservesDiverged.selector, address(x), uint256(1001e18), uint256(1006e18)
            )
        );
        p.swapExactInput(address(x), 1e18, 0, alice, dl);

        p.sync();
        (uint112 r0, uint112 r1, uint256 ts) = p.getReserves();
        assertEq(uint256(r0), 1005e18);
        assertEq(uint256(r1), 1000e18);
        assertEq(ts, supplyBefore, "sync mints no shares");
        assertEq(x.balanceOf(address(p)), uint256(r0));
        assertEq(y.balanceOf(address(p)), uint256(r1));

        // trading resumes and is consistent
        vm.prank(alice);
        uint256 out = p.swapExactInput(address(x), 1e18, 0, alice, dl);
        assertGt(out, 0);
        _assertReserves(p, x, y);
    }

    function test_Sync_BeforeFirstMint_RejectsPrefunded() public {
        TestERC20 x = new TestERC20("X", "X");
        TestERC20 y = new TestERC20("Y", "Y");
        ConstantProductPool p = new ConstantProductPool(address(x), address(y));
        x.mint(alice, 1e18);
        vm.prank(alice);
        x.transfer(address(p), 1e18);
        vm.expectRevert(ConstantProductPool.TokenPreFunded.selector);
        p.sync();
    }

    // ==================================================================
    // Atomicity: a reverted swap refunds input and never pays output
    // ==================================================================
    function test_FailedSwap_FullyAtomic() public {
        TestERC20 x = new TestERC20("X", "X");
        TestERC20 y = new TestERC20("Y", "Y");
        ConstantProductPool p = new ConstantProductPool(address(x), address(y));
        x.mint(alice, 2000e18);
        y.mint(alice, 2000e18);
        _approveMax(address(x), alice, address(p));
        _approveMax(address(y), alice, address(p));
        vm.prank(alice);
        p.addLiquidity(1000e18, 1000e18, 0, alice, dl);

        x.mint(bob, 100e18);
        _approveMax(address(x), bob, address(p));
        uint256 fair = p.quoteAmountOut(address(x), 10e18);

        uint256 bx = x.balanceOf(bob);
        uint256 by = y.balanceOf(bob);
        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(ConstantProductPool.MinOutputNotMet.selector, fair, fair + 1));
        p.swapExactInput(address(x), 10e18, fair + 1, bob, dl);

        assertEq(x.balanceOf(bob), bx, "input returned");
        assertEq(y.balanceOf(bob), by, "no output");
        assertEq(x.balanceOf(address(p)), 1000e18);
        assertEq(y.balanceOf(address(p)), 1000e18);
    }
}

/// @notice CREATE factory for predictable pool addresses.
contract PoolFactory {
    function deploy(address a, address b) external returns (ConstantProductPool p) {
        p = new ConstantProductPool(a, b);
    }
}

/// @notice Fee token that is honest until a test arms it, charging a
///         recipient-side fee only on transfers sent FROM the pool.
contract SwitchableOutFeeToken {
    string public name;
    string public symbol;
    uint8 public constant decimals = 18;
    uint256 public immutable feeBps;
    address public pool;
    bool public charging;

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor(string memory _n, string memory _s, uint256 _bps) {
        name = _n;
        symbol = _s;
        feeBps = _bps;
    }

    function setPool(address _pool) external {
        pool = _pool;
    }

    function setCharging(bool _c) external {
        charging = _c;
    }

    function mint(address to, uint256 amount) external {
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transfer(address to, uint256 value) external returns (bool) {
        _transfer(msg.sender, to, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        if (allowed != type(uint256).max) {
            require(allowed >= value, "allow");
            allowance[from][msg.sender] = allowed - value;
        }
        _transfer(from, to, value);
        return true;
    }

    function _transfer(address from, address to, uint256 value) internal {
        require(balanceOf[from] >= value, "bal");
        uint256 fee = (charging && from == pool) ? (value * feeBps) / 10_000 : 0;
        unchecked {
            balanceOf[from] -= value;
            balanceOf[to] += value - fee;
            totalSupply -= fee;
        }
        emit Transfer(from, to, value - fee);
    }
}
