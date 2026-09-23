// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {CPMMFactory} from "../src/CPMMFactory.sol";
import {CPMMPair} from "../src/CPMMPair.sol";
import {CPMMRouter} from "../src/CPMMRouter.sol";
import {TestERC20} from "../src/mocks/TestERC20.sol";
import {FeeOnTransferERC20} from "../src/mocks/FeeOnTransferERC20.sol";

/// @notice Fee-on-transfer tokens must be rejected at every entry point:
///         first/second mint, and swap (single hop). The rejection is atomic.
contract FeeOnTransferTest is Test {
    CPMMFactory internal factory;
    CPMMRouter internal router;
    TestERC20 internal good;
    FeeOnTransferERC20 internal feeToken;

    address internal alice = address(0xA11CE);

    function setUp() public {
        factory = new CPMMFactory();
        router = new CPMMRouter(address(factory));
        good = new TestERC20("Good", "GOOD");
        feeToken = new FeeOnTransferERC20(30); // 0.30% fee
        good.mint(alice, 1_000_000 ether);
        feeToken.mint(alice, 1_000_000 ether);
        // Bootstrap a healthy GOOD/FEE pool with the plain token on both sides
        // is impossible (FEE charges), so create the pair but do NOT seed it
        // via the fee token. Tests below attempt seeding and expect rejection.
    }

    function test_Mint_RejectsFeeOnTransfer_Bootstrap() public {
        address pair = factory.createPair(address(good), address(feeToken));
        vm.startPrank(alice);
        good.transfer(pair, 100 ether);
        feeToken.transfer(pair, 100 ether); // only 99.97 ether arrives
        uint256 gotFee = feeToken.balanceOf(pair);
        assertLt(gotFee, 100 ether, "mock must short-change");
        (uint256 exp0, uint256 exp1) =
            address(good) < address(feeToken) ? (100 ether, 100 ether) : (100 ether, 100 ether);
        vm.expectRevert(CPMMPair.TransferFailed.selector);
        CPMMPair(pair).mint(alice, exp0, exp1);
        vm.stopPrank();
    }

    function test_Mint_RejectsFeeOnTransfer_SecondDeposit() public {
        // Seed a pool with two GOOD tokens? No — a pair needs distinct tokens.
        // Instead seed GOOD/FEE through a non-fee window by disabling fee is
        // not possible, so build the pool using a separate good token pair and
        // verify swap-path rejection of the fee token directly against its pair
        // after manually establishing reserves with the fee token's gross-up.
        //
        // Establish reserves by sending grossed-up amounts so the *net* credit
        // matches expected exactly once (simulating a pre-existing pool).
        address pair = factory.createPair(address(good), address(feeToken));
        // Send enough so net == 100 ether: gross = ceil(100e18 / 0.997).
        uint256 gross = (uint256(100 ether) * 10_000) / (10_000 - 30) + 1;
        vm.startPrank(alice);
        good.transfer(pair, 100 ether);
        feeToken.transfer(pair, gross);
        // Net fee-token received:
        uint256 net = feeToken.balanceOf(pair);
        // First mint succeeds only if we declare the true net amounts.
        uint256 e0 = address(good) < address(feeToken) ? uint256(100 ether) : net;
        uint256 e1 = address(good) < address(feeToken) ? net : uint256(100 ether);
        CPMMPair(pair).mint(alice, e0, e1);
        vm.stopPrank();

        // A second deposit that declares the nominal amount reverts because the
        // realized delta is smaller (fee charged again).
        vm.startPrank(alice);
        uint256 before = feeToken.balanceOf(pair);
        feeToken.transfer(pair, 100 ether);
        good.transfer(pair, 100 ether);
        uint256 net2 = feeToken.balanceOf(pair) - before;
        assertLt(net2, 100 ether);
        (uint256 x0, uint256 x1) = address(good) < address(feeToken) ? (100 ether, 100 ether) : (100 ether, 100 ether);
        vm.expectRevert(CPMMPair.TransferFailed.selector);
        CPMMPair(pair).mint(alice, x0, x1);
        vm.stopPrank();
    }

    function test_Router_RejectsFeeOnTransfer() public {
        // Router checks the pair balance delta on pull.
        vm.startPrank(alice);
        good.approve(address(router), type(uint256).max);
        feeToken.approve(address(router), type(uint256).max);
        vm.expectRevert(CPMMRouter.FeeOnTransferDetected.selector);
        router.addLiquidity(address(good), address(feeToken), 100 ether, 100 ether, 0, 0, alice, block.timestamp + 1);
        vm.stopPrank();
    }

    function test_Swap_RejectsFeeOnTransferInput() public {
        // Build a pool with GOOD and a zero-fee token, then a separate pool with
        // the fee token seeded grossed-up (as in the second-deposit test) and
        // attempt a swap whose declared input exceeds the realized delta.
        address pair = factory.createPair(address(good), address(feeToken));
        uint256 gross = (uint256(1_000 ether) * 10_000) / (10_000 - 30) + 1;
        vm.startPrank(alice);
        good.transfer(pair, 1_000 ether);
        feeToken.transfer(pair, gross);
        uint256 net = feeToken.balanceOf(pair);
        uint256 e0 = address(good) < address(feeToken) ? uint256(1_000 ether) : net;
        uint256 e1 = address(good) < address(feeToken) ? net : uint256(1_000 ether);
        CPMMPair(pair).mint(alice, e0, e1);
        vm.stopPrank();

        // Swap fee token in: declared 1 ether, but only 0.997 ether arrives.
        vm.startPrank(alice);
        feeToken.transfer(pair, 1 ether);
        bool goodIs0 = address(good) < address(feeToken);
        // Ask a tiny GOOD output so the only failure is the fee shortfall check.
        vm.expectRevert(CPMMPair.TransferFailed.selector);
        if (goodIs0) {
            CPMMPair(pair).swap(1, 0, alice, 0, 1 ether, "");
        } else {
            CPMMPair(pair).swap(0, 1, alice, 1 ether, 0, "");
        }
        vm.stopPrank();
    }
}
