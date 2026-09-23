// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Script} from "forge-vm/Script.sol";
import {console} from "forge-vm/console.sol";
import {PaymentAdjudicator} from "../src/PaymentAdjudicator.sol";
import {TestToken} from "../src/TestToken.sol";

/// @title Demo
/// @notice Fully local, zero-dependency end-to-end walkthrough.
///
///         Run (no node, no network):
///
///             forge script script/Demo.s.sol
///
///         Everything is REAL: secp256k1 signatures produced by vm.sign over
///         the contract's EIP-712 digest, genuine state transitions and
///         block-time control via vm.warp. This is an educational demo of THIS
///         adjudicator only; it is not compatible with the Lightning Network
///         or any other payment-channel protocol.
///
///         Scenario
///         --------
///         1. Payer opens a channel escrowing 1000 test tokens.
///         2. Off-chain, the parties mutually sign states seq 1/2/3 paying
///            cumulatively 100/400/700 tokens (no chain traffic).
///         3. A malicious relayer closes on-chain using the STALE state
///            seq=1 (claiming only 100).
///         4. The payee counters inside the challenge window with seq=2 then
///            seq=3; replaying the stale seq=1 is rejected.
///         5. Settling one second before the deadline is rejected; settling AT
///            the deadline pays the payee 700 and refunds 300 to the payer.
///         6. A duplicate settle is rejected (settlement is one-time).
contract Demo is Script {
    uint256 internal constant PAYER_PK = 0xA11CE;
    uint256 internal constant PAYEE_PK = 0xB0B;

    function run() external {
        address payer = vm.addr(PAYER_PK);
        address payee = vm.addr(PAYEE_PK);

        console.log("==========================================================");
        console.log(" Payment channel adjudication - local end-to-end demo");
        console.log("==========================================================");
        line("payer (funds channel): ", payer);
        line("payee (merchant):      ", payee);
        line("chain id:              ", block.chainid);

        // ---- Deploy test token + adjudicator -------------------------------
        TestToken token = new TestToken("Test Token", "TT", 18);
        PaymentAdjudicator adjudicator = new PaymentAdjudicator();
        line("token:       ", address(token));
        line("adjudicator: ", address(adjudicator));

        uint256 collateral = 1_000 ether;
        uint64 duration = 1 days;

        // ---- Fund and approve ----------------------------------------------
        token.mint(payer, collateral);
        vm.prank(payer);
        token.approve(address(adjudicator), type(uint256).max);

        // ---- 1. Open the channel -------------------------------------------
        bytes32 channelId = adjudicator.computeChannelId(
            payer, payee, address(token), collateral, duration, 1
        );
        vm.prank(payer);
        adjudicator.openChannel(payer, payee, token, collateral, duration, 1);
        line("channel id:    ", channelId);
        line("escrowed (TT): ", collateral / 1 ether);

        // ---- 2. Off-chain state updates (no chain traffic) -----------------
        PaymentAdjudicator.State memory s1 = _state(channelId, 1, 100 ether);
        PaymentAdjudicator.State memory s2 = _state(channelId, 2, 400 ether);
        PaymentAdjudicator.State memory s3 = _state(channelId, 3, 700 ether);
        (bytes memory p1, bytes memory e1) = _signBoth(adjudicator, s1);
        (bytes memory p2, bytes memory e2) = _signBoth(adjudicator, s2);
        (bytes memory p3, bytes memory e3) = _signBoth(adjudicator, s3);
        console.log("off-chain states signed: seq1=100 seq2=400 seq3=700 TT");

        // ---- 3. Malicious close with the STALE state seq=1 -----------------
        vm.prank(payer);
        adjudicator.close(channelId, s1, p1, e1);
        lineTT("[*] malicious close with stale seq=1, payout claimed (TT): ",
            adjudicator.getChannel(channelId).bestCumulativePaid);

        // ---- 4a. Honest payee counters with seq=2 --------------------------
        vm.prank(payee);
        adjudicator.challenge(s2, p2, e2);
        lineTT("[*] payee challenge seq=2 -> payout (TT): ",
            adjudicator.getChannel(channelId).bestCumulativePaid);

        // ---- 4b. Replaying the stale seq=1 is rejected ---------------------
        _mustRevert("replay stale seq=1", _challengeReverted(adjudicator, payee, s1, p1, e1));

        // ---- 4c. Newest seq=3 wins -----------------------------------------
        vm.prank(payee);
        adjudicator.challenge(s3, p3, e3);
        lineTT("[*] payee challenge seq=3 -> payout (TT): ",
            adjudicator.getChannel(channelId).bestCumulativePaid);

        // ---- 5a. Settle before the deadline must fail ----------------------
        uint64 endsAt = adjudicator.getChannel(channelId).challengeEndsAt;
        vm.warp(endsAt - 1);
        _mustRevert("settle 1s before deadline", _settleReverted(adjudicator, payer, channelId));

        // ---- 5b. Settle exactly at the deadline ----------------------------
        vm.warp(endsAt);
        vm.prank(payer);
        adjudicator.settle(channelId);
        lineTT("[*] settled; payee balance (TT):   ", token.balanceOf(payee));
        lineTT("[*] settled; payer refund (TT):    ", token.balanceOf(payer));
        lineTT("[*] adjudicator residual (TT):     ", token.balanceOf(address(adjudicator)));

        // ---- 6. Duplicate settle is rejected -------------------------------
        _mustRevert("duplicate settle", _settleReverted(adjudicator, payer, channelId));

        console.log("==========================================================");
        console.log(" demo complete: newest state won, funds conserved");
        console.log("==========================================================");
    }

    // ----------------------------------------------------------------------
    // Helpers
    // ----------------------------------------------------------------------

    function _state(bytes32 id, uint64 seq, uint256 paid)
        internal
        view
        returns (PaymentAdjudicator.State memory)
    {
        return PaymentAdjudicator.State(id, block.chainid, seq, paid);
    }

    function _signBoth(
        PaymentAdjudicator adjudicator,
        PaymentAdjudicator.State memory state
    ) internal returns (bytes memory payerSig, bytes memory payeeSig) {
        bytes32 digest = adjudicator.stateDigest(state);
        (uint8 pv, bytes32 pr, bytes32 ps) = vm.sign(PAYER_PK, digest);
        (uint8 ev, bytes32 er, bytes32 es) = vm.sign(PAYEE_PK, digest);
        payerSig = abi.encodePacked(pr, ps, pv);
        payeeSig = abi.encodePacked(er, es, ev);
    }

    /// @dev Returns true when the challenge call REVERTED.
    function _challengeReverted(
        PaymentAdjudicator adjudicator,
        address payee,
        PaymentAdjudicator.State memory state,
        bytes memory payerSig,
        bytes memory payeeSig
    ) internal returns (bool reverted) {
        vm.prank(payee);
        try adjudicator.challenge(state, payerSig, payeeSig) {
            return false;
        } catch (bytes memory reason) {
            console.log(string.concat("       rejected with selector ", vm.toString(bytes4(reason))));
            return true;
        }
    }

    function _settleReverted(PaymentAdjudicator adjudicator, address payer, bytes32 channelId)
        internal
        returns (bool reverted)
    {
        vm.prank(payer);
        try adjudicator.settle(channelId) {
            return false;
        } catch (bytes memory reason) {
            console.log(string.concat("       rejected with selector ", vm.toString(bytes4(reason))));
            return true;
        }
    }

    function _mustRevert(string memory what, bool reverted) internal view {
        require(reverted, string.concat("expected revert but call succeeded: ", what));
        console.log(string.concat("       [OK] reverted as expected: ", what));
    }

    function line(string memory label, address a) internal view {
        console.log(string.concat(label, vm.toString(a)));
    }

    function line(string memory label, uint256 x) internal view {
        console.log(string.concat(label, vm.toString(x)));
    }

    function line(string memory label, bytes32 x) internal view {
        console.log(string.concat(label, vm.toString(x)));
    }

    function lineTT(string memory label, uint256 weiAmount) internal view {
        console.log(string.concat(label, vm.toString(weiAmount / 1 ether), " TT"));
    }
}
