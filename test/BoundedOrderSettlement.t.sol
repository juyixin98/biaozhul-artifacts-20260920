// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {BoundedOrderSettlement as Settlement} from "../src/BoundedOrderSettlement.sol";
import {MockERC20} from "../src/MockERC20.sol";

interface Vm {
    function expectRevert() external;
    function expectRevert(bytes4 msg_) external;
    function expectRevert(bytes calldata msg_) external;
    function prank(address) external;
    function startPrank(address) external;
    function stopPrank() external;
    function warp(uint256) external;
    function addr(uint256) external returns (address);
    function sign(uint256, bytes32) external returns (uint8 v, bytes32 r, bytes32 s);
}

contract BoundedOrderSettlementTest {
    Vm constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);

    Settlement internal settlement;
    MockERC20 internal tokenA; // maker token
    MockERC20 internal tokenB; // taker token

    uint256 internal makerPk = 0xA11CE;
    uint256 internal takerPk = 0xB0B;
    uint256 internal feePk = 0xFEE;
    address internal maker;
    address internal taker;
    address internal feeRecipient;

    uint256 constant MINT = 1_000_000e18;

    function setUp() external {
        settlement = new Settlement();
        tokenA = new MockERC20("Mock A", "MKA", 18);
        tokenB = new MockERC20("Mock B", "MKB", 18);
        maker = vm.addr(makerPk);
        taker = vm.addr(takerPk);
        feeRecipient = vm.addr(feePk);

        tokenA.mint(maker, MINT);
        tokenB.mint(taker, MINT);

        vm.prank(maker);
        tokenA.approve(address(settlement), type(uint256).max);
        vm.prank(taker);
        tokenB.approve(address(settlement), type(uint256).max);
    }

    // -------------------------------------------------------------- //
    // helpers                                                        //
    // -------------------------------------------------------------- //

    function _order(
        uint256 makerAmount,
        uint256 takerAmount,
        uint256 nonce,
        uint256 deadline,
        address allowedTaker,
        uint256 maxFee
    ) internal view returns (Settlement.Order memory o) {
        o = Settlement.Order({
            maker: maker,
            taker: allowedTaker,
            makerToken: address(tokenA),
            takerToken: address(tokenB),
            makerAmount: makerAmount,
            takerAmount: takerAmount,
            nonce: nonce,
            deadline: deadline,
            feeRecipient: maxFee > 0 ? feeRecipient : address(0),
            maxFeeAmount: maxFee
        });
    }

    function _digest(Settlement.Order memory o) internal view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(
                settlement.ORDER_TYPEHASH(),
                o.maker,
                o.taker,
                o.makerToken,
                o.takerToken,
                o.makerAmount,
                o.takerAmount,
                o.nonce,
                o.deadline,
                o.feeRecipient,
                o.maxFeeAmount
            )
        );
        return keccak256(abi.encodePacked(hex"1901", settlement.domainSeparator(), structHash));
    }

    function _sign(uint256 pk, Settlement.Order memory o) internal returns (bytes memory sig) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, _digest(o));
        sig = abi.encodePacked(r, s, v);
    }

    function _fill(Settlement.Order memory o, uint256 spent, uint256 fee, bytes memory sig)
        internal
        returns (uint256 due)
    {
        vm.prank(taker);
        due = settlement.fillOrder(o, spent, fee, sig);
    }

    function _expectRevertAndFill(
        Settlement.Order memory o,
        uint256 spent,
        uint256 fee,
        bytes memory sig,
        bytes4 selector
    ) internal {
        vm.prank(taker);
        vm.expectRevert(selector);
        settlement.fillOrder(o, spent, fee, sig);
    }

    function _expectRevertAndFill(
        Settlement.Order memory o,
        uint256 spent,
        uint256 fee,
        bytes memory sig,
        bytes memory reason
    ) internal {
        // prank must come BEFORE expectRevert, then the reverting call next.
        // NOTE: foundry's expectRevert(bytes4) exact-matches 4 bytes of data,
        // so parameterized custom errors must be passed as full encoded bytes.
        vm.prank(taker);
        vm.expectRevert(reason);
        settlement.fillOrder(o, spent, fee, sig);
    }

    // -------------------------------------------------------------- //
    // 1. full fill, basic happy path                                 //
    // -------------------------------------------------------------- //

    function testFullFill() external {
        Settlement.Order memory o =
            _order(100e18, 30e18, 1, block.timestamp + 1 hours, address(0), 0);
        bytes memory sig = _sign(makerPk, o);

        uint256 due = _fill(o, 100e18, 0, sig);
        assertEq(due, 30e18, "taker due");
        assertEq(tokenA.balanceOf(taker), 100e18, "taker A");
        assertEq(tokenB.balanceOf(maker), 30e18, "maker B");
    }

    // -------------------------------------------------------------- //
    // 2. two partial fills with rounding + dust-clearing final fill //
    // -------------------------------------------------------------- //

    /// @dev 100 A for 3 B. Floor division would pay the maker 0 B on small
    /// fills; ceil must charge the taker 1 B each and the final fill clears
    /// the remaining 1 B.
    function testTwoPartialFillsRoundingSmall() external {
        Settlement.Order memory o = _order(100, 3, 7, block.timestamp + 1 hours, address(0), 0);
        tokenA.mint(maker, 1_000);
        tokenB.mint(taker, 1_000);
        bytes memory sig = _sign(makerPk, o);

        uint256 due1 = _fill(o, 33, 0, sig);
        assertEq(due1, 1, "ceil(0.99) = 1");
        uint256 due2 = _fill(o, 33, 0, sig);
        assertEq(due2, 1, "second partial still 1");
        uint256 due3 = _fill(o, 34, 0, sig);
        assertEq(due3, 1, "final fill clears dust: 3 - 2");

        assertEq(tokenB.balanceOf(maker), 3, "maker got full 3 B");
        assertEq(tokenA.balanceOf(taker), 100, "taker got full 100 A");
    }

    /// @dev 600 A for 400 B: two 100-A partial fills, then the remainder.
    function testTwoPartialFillsRoundingLarge() external {
        Settlement.Order memory o = _order(600, 400, 8, block.timestamp + 1 hours, address(0), 0);
        tokenA.mint(maker, 1_000);
        tokenB.mint(taker, 1_000);
        bytes memory sig = _sign(makerPk, o);

        assertEq(_fill(o, 100, 0, sig), 67, "ceil(66.66) = 67");
        assertEq(_fill(o, 100, 0, sig), 67, "second = 67");
        assertEq(_fill(o, 400, 0, sig), 266, "final = 400 - 134");
        assertEq(tokenB.balanceOf(maker), 400, "maker B total");
    }

    // -------------------------------------------------------------- //
    // 3. conservation + signed-amount bounds across fee-bearing fills//
    // -------------------------------------------------------------- //

    function testConservationAndBoundsWithFees() external {
        uint256 makerAmount = 1_000e18;
        uint256 takerAmount = 250e18;
        uint256 maxFee = 10e18;
        Settlement.Order memory o =
            _order(makerAmount, takerAmount, 2, block.timestamp + 1 hours, address(0), maxFee);
        bytes memory sig = _sign(makerPk, o);

        uint256[4] memory spent =
            [uint256(250e18), uint256(250e18), uint256(249e18), uint256(251e18)];
        uint256[4] memory fees = [uint256(3e18), uint256(2e18), uint256(2e18), uint256(3e18)]; // sums to 10 = cap

        uint256 makerABefore = tokenA.balanceOf(maker);
        uint256 takerABefore = tokenA.balanceOf(taker);
        uint256 feeABefore = tokenA.balanceOf(feeRecipient);
        uint256 makerBBefore = tokenB.balanceOf(maker);
        uint256 takerBBefore = tokenB.balanceOf(taker);

        uint256 totalTakerDue;
        for (uint256 i = 0; i < 4; i++) {
            totalTakerDue += _fill(o, spent[i], fees[i], sig);
        }

        // signed-amount bounds
        bytes32 h = settlement.hashOrder(o);
        assertEq(settlement.filledMakerAmount(h), makerAmount, "spent == signed maker amount");
        assertEq(settlement.cumulativeFee(h), maxFee, "fees == signed cap");
        assertTrue(totalTakerDue <= takerAmount, "taker never pays more than signed");
        assertTrue(totalTakerDue >= takerAmount - 3, "taker pays within rounding of signed");

        // conservation of A: maker loss == taker gain + fee gain
        uint256 makerALost = makerABefore - tokenA.balanceOf(maker);
        assertEq(
            makerALost,
            (tokenA.balanceOf(taker) - takerABefore)
                + (tokenA.balanceOf(feeRecipient) - feeABefore),
            "A conserved"
        );
        assertEq(makerALost, makerAmount, "maker lost exactly signed amount");

        // conservation of B: taker loss == maker gain
        assertEq(
            takerBBefore - tokenB.balanceOf(taker),
            tokenB.balanceOf(maker) - makerBBefore,
            "B conserved"
        );
        assertEq(tokenB.balanceOf(maker), makerBBefore + totalTakerDue, "maker received due");
    }

    function testFeeCapRevert() external {
        Settlement.Order memory o = _order(1_000, 250, 3, block.timestamp + 1 hours, address(0), 5);
        bytes memory sig = _sign(makerPk, o);
        _expectRevertAndFill(
            o,
            100,
            6,
            sig,
            abi.encodeWithSelector(Settlement.FeeExceedsCap.selector, uint256(6), uint256(5))
        );
    }

    function testCumulativeFeeCapRevert() external {
        Settlement.Order memory o = _order(1_000, 250, 4, block.timestamp + 1 hours, address(0), 5);
        bytes memory sig = _sign(makerPk, o);
        _fill(o, 500, 3, sig);
        // 3+3 > 5 cumulative; remaining cap is 5-3 = 2
        _expectRevertAndFill(
            o,
            500,
            3,
            sig,
            abi.encodeWithSelector(Settlement.FeeExceedsCap.selector, uint256(3), uint256(2))
        );
    }

    function testFeeExceedsSpentRevert() external {
        Settlement.Order memory o = _order(1_000, 250, 9, block.timestamp + 1 hours, address(0), 50);
        bytes memory sig = _sign(makerPk, o);
        _expectRevertAndFill(
            o,
            10,
            11,
            sig,
            abi.encodeWithSelector(Settlement.FeeExceedsSpent.selector, uint256(11), uint256(10))
        );
    }

    // -------------------------------------------------------------- //
    // 4. cancellation races                                          //
    // -------------------------------------------------------------- //

    function testCancelBlocksFill() external {
        Settlement.Order memory o = _order(100, 30, 11, block.timestamp + 1 hours, address(0), 0);
        bytes memory sig = _sign(makerPk, o);

        vm.prank(maker);
        settlement.cancelNonce(11);

        _expectRevertAndFill(o, 100, 0, sig, Settlement.NonceCancelled.selector);
    }

    /// @dev A third party "cancelling" the same numeric nonce only sets their
    /// own slot; the maker's order must remain fillable.
    function testCancelRaceByWrongAddressHasNoEffect() external {
        Settlement.Order memory o = _order(100, 30, 12, block.timestamp + 1 hours, address(0), 0);
        bytes memory sig = _sign(makerPk, o);

        vm.prank(taker);
        settlement.cancelNonce(12); // taker cancels THEIR nonce 12

        _fill(o, 100, 0, sig); // maker's nonce 12 unaffected
        assertEq(tokenB.balanceOf(maker), 30, "order still settled");
    }

    function testCancelAfterPartialBlocksRest() external {
        Settlement.Order memory o = _order(100, 30, 13, block.timestamp + 1 hours, address(0), 0);
        bytes memory sig = _sign(makerPk, o);

        _fill(o, 40, 0, sig);
        vm.prank(maker);
        settlement.cancelNonce(13);
        _expectRevertAndFill(o, 40, 0, sig, Settlement.NonceCancelled.selector);
        assertEq(tokenB.balanceOf(maker), 12, "first partial (ceil 12) stands");
    }

    // -------------------------------------------------------------- //
    // 5. replay protection                                           //
    // -------------------------------------------------------------- //

    function testReplayAfterFullFillReverts() external {
        Settlement.Order memory o = _order(100, 30, 21, block.timestamp + 1 hours, address(0), 0);
        bytes memory sig = _sign(makerPk, o);

        _fill(o, 100, 0, sig);
        _expectRevertAndFill(
            o,
            100,
            0,
            sig,
            abi.encodeWithSelector(Settlement.FillExceedsOrder.selector, uint256(100), uint256(0))
        );
    }

    function testReplayOverfillAcrossPartialsReverts() external {
        Settlement.Order memory o = _order(100, 30, 22, block.timestamp + 1 hours, address(0), 0);
        bytes memory sig = _sign(makerPk, o);

        _fill(o, 60, 0, sig);
        _expectRevertAndFill(
            o,
            60,
            0,
            sig,
            abi.encodeWithSelector(Settlement.FillExceedsOrder.selector, uint256(60), uint256(40))
        );
        // exact remaining still works
        _fill(o, 40, 0, sig);
        assertEq(tokenB.balanceOf(maker), 30, "exact remainder accepted");
    }

    /// @dev Reusing a cancelled nonce number under a NEW order content still
    /// fails: cancellation keyed on (maker, nonce), not order hash.
    function testNewOrderOnCancelledNonceReverts() external {
        Settlement.Order memory o1 = _order(100, 30, 23, block.timestamp + 1 hours, address(0), 0);
        bytes memory sig1 = _sign(makerPk, o1);
        vm.prank(maker);
        settlement.cancelNonce(23);
        _expectRevertAndFill(o1, 100, 0, sig1, Settlement.NonceCancelled.selector);
    }

    // -------------------------------------------------------------- //
    // 6. signature / domain binding                                  //
    // -------------------------------------------------------------- //

    /// @dev A signature over THIS settlement's domain is invalid on a second
    /// deployment: the EIP-712 domain binds the contract address.
    function testSignatureInvalidOnDifferentDeployment() external {
        Settlement settlement2 = new Settlement();
        Settlement.Order memory o = _order(100, 30, 31, block.timestamp + 1 hours, address(0), 0);
        bytes memory sig = _sign(makerPk, o); // signed against `settlement`

        vm.prank(taker);
        vm.expectRevert(Settlement.InvalidSignature.selector);
        settlement2.fillOrder(o, 100, 0, sig);
    }

    function testWrongSignerReverts() external {
        Settlement.Order memory o = _order(100, 30, 32, block.timestamp + 1 hours, address(0), 0);
        bytes memory sig = _sign(takerPk, o); // taker signs maker's order
        _expectRevertAndFill(o, 100, 0, sig, Settlement.InvalidSignature.selector);
    }

    function testTamperedOrderReverts() external {
        Settlement.Order memory o = _order(100, 30, 33, block.timestamp + 1 hours, address(0), 0);
        bytes memory sig = _sign(makerPk, o);
        o.takerAmount = 29; // mutate after signing
        _expectRevertAndFill(o, 100, 0, sig, Settlement.InvalidSignature.selector);
    }

    function testHighSmalleableSignatureRejected() external {
        Settlement.Order memory o = _order(100, 30, 34, block.timestamp + 1 hours, address(0), 0);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(makerPk, _digest(o));
        // high-s counterpart: s' = secp256k1n - s, flip v parity
        uint256 N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;
        bytes32 sHigh = bytes32(N - uint256(s));
        uint8 vAlt = v == 27 ? 28 : 27;
        bytes memory badSig = abi.encodePacked(r, sHigh, vAlt);
        _expectRevertAndFill(o, 100, 0, badSig, Settlement.InvalidSignature.selector);
    }

    // -------------------------------------------------------------- //
    // 7. expiry, taker allow-list, zero amounts                      //
    // -------------------------------------------------------------- //

    function testExpiredOrderReverts() external {
        uint256 deadline = block.timestamp + 1 hours;
        Settlement.Order memory o = _order(100, 30, 41, deadline, address(0), 0);
        bytes memory sig = _sign(makerPk, o);
        vm.warp(deadline + 1);
        _expectRevertAndFill(o, 100, 0, sig, Settlement.OrderExpired.selector);
    }

    function testFillsExactlyAtDeadline() external {
        uint256 deadline = block.timestamp + 1 hours;
        Settlement.Order memory o = _order(100, 30, 42, deadline, address(0), 0);
        bytes memory sig = _sign(makerPk, o);
        vm.warp(deadline); // boundary: timestamp == deadline is valid
        _fill(o, 100, 0, sig);
    }

    function testDesignatedTakerEnforced() external {
        address other = vm.addr(0x074E);
        Settlement.Order memory o = _order(100, 30, 43, block.timestamp + 1 hours, other, 0);
        bytes memory sig = _sign(makerPk, o);
        _expectRevertAndFill(o, 100, 0, sig, Settlement.UnauthorizedTaker.selector);

        vm.prank(other);
        vm.expectRevert(); // other has no B approval -> transferFrom reverts
        settlement.fillOrder(o, 100, 0, sig);
    }

    function testZeroSpentReverts() external {
        Settlement.Order memory o = _order(100, 30, 44, block.timestamp + 1 hours, address(0), 0);
        bytes memory sig = _sign(makerPk, o);
        _expectRevertAndFill(o, 0, 0, sig, Settlement.ZeroAmount.selector);
    }

    function testFeeWithoutRecipientReverts() external {
        Settlement.Order memory o = _order(100, 30, 45, block.timestamp + 1 hours, address(0), 0);
        bytes memory sig = _sign(makerPk, o);
        _expectRevertAndFill(o, 100, 1, sig, Settlement.FeeRecipientRequired.selector);
    }

    // -------------------------------------------------------------- //
    // 8. token transfer failures roll back everything                //
    // -------------------------------------------------------------- //

    function testMakerWithoutAllowanceReverts() external {
        Settlement.Order memory o = _order(100, 30, 51, block.timestamp + 1 hours, address(0), 0);
        bytes memory sig = _sign(makerPk, o);

        // fresh maker who never approved the settlement
        uint256 freshPk = 0xF2E54;
        address fresh = vm.addr(freshPk);
        tokenA.mint(fresh, 100);
        o.maker = fresh;
        sig = _sign(freshPk, o);

        uint256 makerBBefore = tokenB.balanceOf(maker);
        _expectRevertAndFill(
            o,
            100,
            0,
            sig,
            abi.encodeWithSignature("Error(string)", "ERC20: insufficient allowance")
        );
        // no state leaked: order hash never filled
        assertEq(settlement.filledMakerAmount(settlement.hashOrder(o)), 0, "no fill recorded");
        assertEq(tokenB.balanceOf(maker), makerBBefore, "no B taken from taker");
    }

    function testTakerTransferFailureRollsBackMakerTransfer() external {
        Settlement.Order memory o = _order(100, 30, 52, block.timestamp + 1 hours, address(0), 0);
        bytes memory sig = _sign(makerPk, o);

        // a taker with zero B balance and zero approval
        address brokeTaker = vm.addr(0xB201E);
        uint256 makerABefore = tokenA.balanceOf(maker);
        uint256 brokeABefore = tokenA.balanceOf(brokeTaker);

        vm.prank(brokeTaker);
        vm.expectRevert(); // ERC20 insufficient balance
        settlement.fillOrder(o, 100, 0, sig);

        // atomicity: the maker->taker A transfer that ran first is rolled back
        assertEq(tokenA.balanceOf(maker), makerABefore, "maker A unchanged");
        assertEq(tokenA.balanceOf(brokeTaker), brokeABefore, "taker received nothing");
        assertEq(settlement.filledMakerAmount(settlement.hashOrder(o)), 0, "fill rolled back");
    }

    // -------------------------------------------------------------- //
    // tiny assertion helpers (no forge-std dependency)              //
    // -------------------------------------------------------------- //

    function assertEq(uint256 a, uint256 b, string memory what) internal pure {
        require(a == b, what);
    }

    function assertTrue(bool cond, string memory what) internal pure {
        require(cond, what);
    }
}
