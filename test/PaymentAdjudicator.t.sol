// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-vm/Test.sol";
import {console} from "forge-vm/console.sol";
import {PaymentAdjudicator} from "../src/PaymentAdjudicator.sol";
import {TestToken} from "../src/TestToken.sol";

/// @notice End-to-end adjudication tests using REAL secp256k1 signatures
///         (vm.sign) and REAL chain-time control (vm.warp / vm.chainId).
contract PaymentAdjudicatorTest is Test {
    PaymentAdjudicator internal adjudicator;
    TestToken internal token;

    uint256 internal constant PAYER_PK = 111;
    uint256 internal constant PAYEE_PK = 222;
    address internal payer;
    address internal payee;

    uint64 internal constant DURATION = 1 days;
    uint256 internal constant COLLATERAL = 1_000 ether;

    uint256 internal chainId;

    function setUp() public {
        adjudicator = new PaymentAdjudicator();
        token = new TestToken("Test Token", "TT", 18);
        payer = vm.addr(PAYER_PK);
        payee = vm.addr(PAYEE_PK);
        vm.label(payer, "payer");
        vm.label(payee, "payee");

        token.mint(payer, COLLATERAL);
        vm.prank(payer);
        token.approve(address(adjudicator), type(uint256).max);

        chainId = block.chainid;
    }

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    function _open(uint256 collateral, uint64 duration) internal returns (bytes32 id) {
        id = adjudicator.computeChannelId(
            payer, payee, address(token), collateral, duration, 1
        );
        adjudicator.openChannel(payer, payee, token, collateral, duration, 1);
    }

    function _open() internal returns (bytes32 id) {
        return _open(COLLATERAL, DURATION);
    }

    function _state(bytes32 id, uint64 seq, uint256 paid)
        internal
        view
        returns (PaymentAdjudicator.State memory)
    {
        return PaymentAdjudicator.State({
            channelId: id,
            chainId: chainId,
            sequence: seq,
            cumulativePaid: paid
        });
    }

    function _signedState(bytes32 id, uint64 seq, uint256 paid)
        internal
        returns (
            PaymentAdjudicator.State memory state,
            bytes memory payerSig,
            bytes memory payeeSig
        )
    {
        state = _state(id, seq, paid);
        bytes32 digest = adjudicator.stateDigest(state);
        (uint8 pv, bytes32 pr, bytes32 ps) = vm.sign(PAYER_PK, digest);
        (uint8 ev, bytes32 er, bytes32 es) = vm.sign(PAYEE_PK, digest);
        payerSig = abi.encodePacked(pr, ps, pv);
        payeeSig = abi.encodePacked(er, es, ev);
    }

    function _close(bytes32 id, uint64 seq, uint256 paid) internal {
        (PaymentAdjudicator.State memory st, bytes memory ps, bytes memory es) =
            _signedState(id, seq, paid);
        vm.prank(payer);
        adjudicator.close(id, st, ps, es);
    }

    function _challenge(bytes32 id, uint64 seq, uint256 paid) internal {
        (PaymentAdjudicator.State memory st, bytes memory ps, bytes memory es) =
            _signedState(id, seq, paid);
        vm.prank(payee);
        adjudicator.challenge(st, ps, es);
    }

    /// @dev Build the signed calldata for a challenge WITHOUT submitting it,
    ///      so tests can arm expectRevert after all view/sign work is done.
    function _challengeData(bytes32 id, uint64 seq, uint256 paid)
        internal
        returns (PaymentAdjudicator.State memory st, bytes memory ps, bytes memory es)
    {
        return _signedState(id, seq, paid);
    }

    function _submitChallenge(PaymentAdjudicator.State memory st, bytes memory ps, bytes memory es)
        internal
    {
        vm.prank(payee);
        adjudicator.challenge(st, ps, es);
    }

    /// @dev Signed close calldata without submitting (see {_challengeData}).
    function _closeData(bytes32 id, uint64 seq, uint256 paid)
        internal
        returns (PaymentAdjudicator.State memory st, bytes memory ps, bytes memory es)
    {
        return _signedState(id, seq, paid);
    }

    function _submitClose(bytes32 id, PaymentAdjudicator.State memory st, bytes memory ps, bytes memory es)
        internal
    {
        vm.prank(payer);
        adjudicator.close(id, st, ps, es);
    }

    function _warpToSettlement(bytes32 id) internal {
        PaymentAdjudicator.Channel memory ch = adjudicator.getChannel(id);
        vm.warp(ch.challengeEndsAt);
    }

    // -----------------------------------------------------------------------
    // Opening / funding
    // -----------------------------------------------------------------------

    function test_Open_FundsEscrowed() public {
        bytes32 id = _open();
        PaymentAdjudicator.Channel memory ch = adjudicator.getChannel(id);

        assertEq(ch.payer, payer);
        assertEq(ch.payee, payee);
        assertEq(ch.collateral, COLLATERAL);
        assertEq(uint256(ch.status), uint256(PaymentAdjudicator.Status.Open));
        assertEq(token.balanceOf(address(adjudicator)), COLLATERAL);
        assertEq(token.balanceOf(payer), 0);
    }

    function test_Open_RevertOnZeroCollateral() public {
        expectError(PaymentAdjudicator.ZeroCollateral.selector);
        adjudicator.openChannel(payer, payee, token, 0, DURATION, 2);
    }

    function test_Open_RevertOnShortDuration() public {
        expectRevertAny();
        adjudicator.openChannel(payer, payee, token, COLLATERAL, 30, 3);
    }

    function test_Open_RevertOnSameParties() public {
        expectErrorData(abi.encodeWithSelector(PaymentAdjudicator.SameParties.selector, payer));
        adjudicator.openChannel(payer, payer, token, COLLATERAL, DURATION, 4);
    }

    function test_Open_DifferentNonceProducesDifferentChannel() public {
        bytes32 id1 = _open();
        token.mint(payer, COLLATERAL);
        bytes32 id2 = adjudicator.computeChannelId(
            payer, payee, address(token), COLLATERAL, DURATION, 999
        );
        assertTrue(id1 != id2, "nonce must change channel id");
        adjudicator.openChannel(payer, payee, token, COLLATERAL, DURATION, 999);
        assertEq(uint256(adjudicator.getChannel(id2).status), uint256(PaymentAdjudicator.Status.Open));
    }

    // -----------------------------------------------------------------------
    // Close / challenge signature validation
    // -----------------------------------------------------------------------

    function test_Close_ValidState_EntersChallenge() public {
        bytes32 id = _open();
        _close(id, 1, 100 ether);

        PaymentAdjudicator.Channel memory ch = adjudicator.getChannel(id);
        assertEq(uint256(ch.status), uint256(PaymentAdjudicator.Status.Challenged));
        assertEq(uint256(ch.bestSequence), 1);
        assertEq(ch.bestCumulativePaid, 100 ether);
        assertEq(ch.challengeEndsAt, uint64(block.timestamp) + DURATION);
    }

    function test_Close_RevertOnTamperedPayerSignature() public {
        bytes32 id = _open();
        (PaymentAdjudicator.State memory st, bytes memory ps, bytes memory es) =
            _signedState(id, 1, 100 ether);

        // Flip one bit of s in the payer signature: recovery must not match payer.
        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly ("memory-safe") {
            r := mload(add(ps, 0x20))
            s := mload(add(ps, 0x40))
            v := byte(0, mload(add(ps, 0x60)))
        }
        s = bytes32(uint256(s) ^ 1);
        // If the flip happened to push s out of the canonical half, flip a higher bit.
        ps = abi.encodePacked(r, s, v);

        vm.prank(payer);
        expectError(PaymentAdjudicator.BadPayerSignature.selector);
        adjudicator.close(id, st, ps, es);
    }

    function test_Close_RevertOnPayeeSignatureAsPayer() public {
        bytes32 id = _open();
        (PaymentAdjudicator.State memory st, , bytes memory es) =
            _signedState(id, 1, 100 ether);
        // Swap signatures: roles are fixed, the payee signature must not pass
        // the payer check.
        vm.prank(payer);
        expectError(PaymentAdjudicator.BadPayerSignature.selector);
        adjudicator.close(id, st, es, es);
    }

    function test_Close_RevertOnBogusSignatureLength() public {
        bytes32 id = _open();
        PaymentAdjudicator.State memory st = _state(id, 1, 100 ether);
        bytes memory bogus = new bytes(32);
        vm.prank(payer);
        expectRevertAny();
        adjudicator.close(id, st, bogus, bogus);
    }

    function test_Close_RevertOnNonPartyCaller() public {
        bytes32 id = _open();
        (PaymentAdjudicator.State memory st, bytes memory ps, bytes memory es) =
            _signedState(id, 1, 100 ether);
        address outsider = address(0xBEEF);
        vm.prank(outsider);
        expectErrorData(abi.encodeWithSelector(PaymentAdjudicator.NotChannelParty.selector, outsider));
        adjudicator.close(id, st, ps, es);
    }

    // -----------------------------------------------------------------------
    // Cross-channel and cross-chain replay
    // -----------------------------------------------------------------------

    function test_Close_RejectCrossChannelReplay() public {
        bytes32 idA = _open();
        // Open a second channel with another nonce; fund it as well.
        token.mint(payer, COLLATERAL);
        bytes32 idB = adjudicator.computeChannelId(
            payer, payee, address(token), COLLATERAL, DURATION, 7
        );
        adjudicator.openChannel(payer, payee, token, COLLATERAL, DURATION, 7);

        // State signed for channel A must not be accepted by channel B.
        (PaymentAdjudicator.State memory stA, bytes memory ps, bytes memory es) =
            _signedState(idA, 1, 100 ether);

        // Directly via close with B's id: channelId field inside the state mismatches.
        vm.prank(payer);
        expectErrorData(abi.encodeWithSelector(PaymentAdjudicator.WrongChannel.selector, idA, idB));
        adjudicator.close(idB, stA, ps, es);

        // Even if the caller rewrites the envelope id, the SIGNATURE no longer
        // verifies because channelId is part of the signed digest.
        PaymentAdjudicator.State memory forged = _state(idB, 1, 100 ether);
        vm.prank(payer);
        expectError(PaymentAdjudicator.BadPayerSignature.selector);
        adjudicator.close(idB, forged, ps, es);
    }

    function test_Close_RejectCrossChainReplay() public {
        bytes32 id = _open();
        // Genuinely sign the state AS IF we were on chain 99999: switch the
        // chain id so the EIP-712 DOMAIN separator also binds to 99999, then
        // switch back and attempt to use that foreign signature here.
        PaymentAdjudicator.State memory foreign = _state(id, 1, 100 ether);
        foreign.chainId = 99999;

        bytes memory payerSig;
        bytes memory payeeSig;
        vm.chainId(99999);
        {
            bytes32 digestForeign = adjudicator.stateDigest(foreign);
            (uint8 pv, bytes32 pr, bytes32 ps2) = vm.sign(PAYER_PK, digestForeign);
            (uint8 ev, bytes32 er, bytes32 es2) = vm.sign(PAYEE_PK, digestForeign);
            payerSig = abi.encodePacked(pr, ps2, pv);
            payeeSig = abi.encodePacked(er, es2, ev);
        }
        vm.chainId(chainId);

        vm.prank(payer);
        expectErrorData(abi.encodeWithSelector(
            PaymentAdjudicator.WrongChainId.selector, uint256(99999), block.chainid
        ));
        adjudicator.close(id, foreign, payerSig, payeeSig);
    }

    // -----------------------------------------------------------------------
    // Monotonicity inside the challenge period
    // -----------------------------------------------------------------------

    function test_Challenge_AcceptsHigherSequenceAndAmount() public {
        bytes32 id = _open();
        _close(id, 1, 100 ether);
        _challenge(id, 2, 250 ether);

        PaymentAdjudicator.Channel memory ch = adjudicator.getChannel(id);
        assertEq(uint256(ch.bestSequence), 2);
        assertEq(ch.bestCumulativePaid, 250 ether);
    }

    function test_Challenge_RejectLowerSequence() public {
        bytes32 id = _open();
        _close(id, 5, 500 ether);
        // Old state that a dishonest party might replay.
        (PaymentAdjudicator.State memory st, bytes memory ps, bytes memory es) =
            _challengeData(id, 4, 100 ether);
        expectErrorData(abi.encodeWithSelector(
            PaymentAdjudicator.StaleSequence.selector, uint64(4), uint64(5)
        ));
        _submitChallenge(st, ps, es);
    }

    function test_Challenge_RejectEqualSequence() public {
        bytes32 id = _open();
        _close(id, 5, 500 ether);
        (PaymentAdjudicator.State memory st, bytes memory ps, bytes memory es) =
            _challengeData(id, 5, 500 ether);
        expectRevertAny();
        _submitChallenge(st, ps, es);
    }

    function test_Challenge_RejectAmountReversionEvenWithHigherSequence() public {
        bytes32 id = _open();
        _close(id, 2, 500 ether);
        // seq is higher but cumulative amount moved backwards.
        (PaymentAdjudicator.State memory st, bytes memory ps, bytes memory es) =
            _challengeData(id, 3, 499 ether);
        expectErrorData(abi.encodeWithSelector(
            PaymentAdjudicator.AmountReverted.selector, uint256(499 ether), uint256(500 ether)
        ));
        _submitChallenge(st, ps, es);
    }

    function test_Challenge_AcceptsEqualAmountHigherSequence() public {
        // A higher sequence with an unchanged cumulative amount is legitimate
        // (e.g. re-signed heartbeat); only backwards movement is forbidden.
        bytes32 id = _open();
        _close(id, 1, 300 ether);
        _challenge(id, 2, 300 ether);
        assertEq(adjudicator.getChannel(id).bestCumulativePaid, 300 ether);
    }

    function test_Challenge_OldStateCannotOverwriteLatest() public {
        // Full "old-state close" scenario: the attacker closes with a stale
        // state, the victim counters with the newest state during the window.
        bytes32 id = _open();

        // Attacker closes using seq=1 (old), honest current state is seq=3.
        _close(id, 1, 100 ether);
        uint64 firstDeadline = adjudicator.getChannel(id).challengeEndsAt;

        // seq=2 intermediate, deadline refreshes.
        vm.warp(firstDeadline - 100);
        _challenge(id, 2, 400 ether);

        // Replaying seq=1 again is rejected.
        (PaymentAdjudicator.State memory oldSt, bytes memory oldPs, bytes memory oldEs) =
            _challengeData(id, 1, 100 ether);
        expectErrorData(abi.encodeWithSelector(
            PaymentAdjudicator.StaleSequence.selector, uint64(1), uint64(2)
        ));
        _submitChallenge(oldSt, oldPs, oldEs);

        // Latest state wins.
        _challenge(id, 3, 700 ether);
        PaymentAdjudicator.Channel memory ch = adjudicator.getChannel(id);
        assertEq(uint256(ch.bestSequence), 3);
        assertEq(ch.bestCumulativePaid, 700 ether);
    }

    function test_Challenge_RevertWhileOpen() public {
        bytes32 id = _open();
        (PaymentAdjudicator.State memory st, bytes memory ps, bytes memory es) =
            _challengeData(id, 1, 100 ether);
        expectErrorData(abi.encodeWithSelector(
            PaymentAdjudicator.NotChallenged.selector, id, uint8(PaymentAdjudicator.Status.Open)
        ));
        _submitChallenge(st, ps, es);
    }

    function test_Close_RevertWhileAlreadyChallenged() public {
        bytes32 id = _open();
        _close(id, 1, 100 ether);
        (PaymentAdjudicator.State memory st, bytes memory ps, bytes memory es) =
            _closeData(id, 2, 200 ether);
        expectErrorData(abi.encodeWithSelector(
            PaymentAdjudicator.NotOpen.selector, id, uint8(PaymentAdjudicator.Status.Challenged)
        ));
        _submitClose(id, st, ps, es);
    }

    // -----------------------------------------------------------------------
    // startChallenge (cooperative offline exit, payout starts at zero)
    // -----------------------------------------------------------------------

    function test_StartChallenge_RevertForOutsider() public {
        bytes32 id = _open();
        vm.prank(address(0xBEEF));
        expectErrorData(abi.encodeWithSelector(PaymentAdjudicator.NotChannelParty.selector, address(0xBEEF)));
        adjudicator.startChallenge(id);
    }

    function test_StartChallenge_CanBeOverturnedBySignedState() public {
        bytes32 id = _open();
        vm.prank(payer);
        adjudicator.startChallenge(id);
        assertEq(adjudicator.getChannel(id).bestCumulativePaid, 0);
        // Payee protects itself with a real signed state.
        _challenge(id, 1, 350 ether);
        assertEq(adjudicator.getChannel(id).bestCumulativePaid, 350 ether);
    }

    // -----------------------------------------------------------------------
    // Settlement & time boundary
    // -----------------------------------------------------------------------

    function test_Settle_RevertBeforeDeadline() public {
        bytes32 id = _open();
        _close(id, 1, 400 ether);

        uint64 endsAt = adjudicator.getChannel(id).challengeEndsAt;
        vm.warp(endsAt - 1);
        expectErrorData(abi.encodeWithSelector(
            PaymentAdjudicator.ChallengePeriodActive.selector,
            uint64(endsAt),
            uint256(endsAt - 1)
        ));
        vm.prank(payer);
        adjudicator.settle(id);
    }

    function test_Settle_SucceedsExactlyAtDeadline() public {
        bytes32 id = _open();
        _close(id, 1, 400 ether);
        _warpToSettlement(id);
        vm.prank(payer);
        adjudicator.settle(id);

        assertEq(uint256(adjudicator.getChannel(id).status), uint256(PaymentAdjudicator.Status.Settled));
        assertEq(token.balanceOf(payee), 400 ether);
        assertEq(token.balanceOf(payer), COLLATERAL - 400 ether);
    }

    function test_Settle_RevertOnDoubleSettle() public {
        bytes32 id = _open();
        _close(id, 1, 400 ether);
        _warpToSettlement(id);
        vm.prank(payer);
        adjudicator.settle(id);
        expectErrorData(abi.encodeWithSelector(PaymentAdjudicator.AlreadySettled.selector, id));
        vm.prank(payer);
        adjudicator.settle(id);
    }

    function test_Settle_PayoutCappedAtCollateral_Fuzz(uint256 promised) public {
        // Any state, however large, can never make the contract pay out more
        // than the escrowed collateral.
        promised = bound(promised, 0, 5_000 ether);
        bytes32 id = _open();
        _close(id, 1, promised);
        _warpToSettlement(id);
        vm.prank(payer);
        adjudicator.settle(id);

        uint256 expectedPayee = promised > COLLATERAL ? COLLATERAL : promised;
        assertEq(token.balanceOf(payee), expectedPayee);
        assertEq(token.balanceOf(payer), COLLATERAL - expectedPayee);
        // Conservation: the adjudicator holds nothing afterwards.
        assertEq(token.balanceOf(address(adjudicator)), 0);
    }

    function test_Settle_StartChallengeWithNoStateRefundsEverything() public {
        bytes32 id = _open();
        vm.prank(payer);
        adjudicator.startChallenge(id);
        _warpToSettlement(id);
        vm.prank(payer);
        adjudicator.settle(id);

        assertEq(token.balanceOf(payee), 0);
        assertEq(token.balanceOf(payer), COLLATERAL);
    }

    function test_Settle_UnknownChannelReverts() public {
        expectErrorData(abi.encodeWithSelector(PaymentAdjudicator.UnknownChannel.selector, bytes32(uint256(0xDEAD))));
        adjudicator.settle(bytes32(uint256(0xDEAD)));
    }

    // -----------------------------------------------------------------------
    // Complete lifecycle
    // -----------------------------------------------------------------------

    function test_FullLifecycle_MultipleUpdatesThenSettle() public {
        bytes32 id = _open();

        // Off-chain states accumulate without any chain interaction.
        _close(id, 1, 100 ether);
        _challenge(id, 2, 250 ether);
        _challenge(id, 3, 250 ether); // heartbeat
        _challenge(id, 4, 900 ether);

        PaymentAdjudicator.Channel memory ch = adjudicator.getChannel(id);
        assertEq(uint256(ch.bestSequence), 4);
        assertEq(ch.bestCumulativePaid, 900 ether);

        vm.warp(ch.challengeEndsAt);
        vm.prank(payer);
        adjudicator.settle(id);

        assertEq(token.balanceOf(payee), 900 ether);
        assertEq(token.balanceOf(payer), 100 ether);
        assertEq(token.balanceOf(address(adjudicator)), 0);
    }

    // Small local replacement for forge-std's bound() used by the fuzz test.
    function bound(uint256 x, uint256 min, uint256 max) internal pure returns (uint256) {
        require(max >= min, "bound: max < min");
        return min + (x % (max - min + 1));
    }
}
