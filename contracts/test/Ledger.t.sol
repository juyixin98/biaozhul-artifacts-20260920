// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Ledger} from "../src/Ledger.sol";

/// @notice Standalone Foundry test (no forge-std dependency): forge treats every
///         `test*` function as a test case and reports failure on EVM revert.
///         `deal`/`prank` cheatcodes are provided by the forge test runner itself.
interface Vm {
    function prank(address) external;
    function expectRevert(bytes calldata) external;
}

contract LedgerTest {
    Vm constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);

    Ledger ledger;
    address alice = address(0xA11CE);
    address bob = address(0xB0B);

    constructor() {
        ledger = new Ledger();
    }

    function _assertEq(uint256 a, uint256 b) internal pure {
        require(a == b, "assertEq failed");
    }

    function test_depositWithdrawAndTransfer() public {
        vm.prank(alice);
        ledger.deposit(100, 1);
        _assertEq(ledger.balances(alice), 100);

        vm.prank(alice);
        ledger.transfer(bob, 30, 2);
        _assertEq(ledger.balances(alice), 70);
        _assertEq(ledger.balances(bob), 30);

        vm.prank(bob);
        ledger.withdraw(10, 3);
        _assertEq(ledger.balances(bob), 20);
    }

    function test_withdrawRevertsOnInsufficientBalance() public {
        bytes memory expected = abi.encodeWithSelector(
            Ledger.InsufficientBalance.selector, alice, uint256(0), uint256(1)
        );
        vm.prank(alice);
        vm.expectRevert(expected);
        ledger.withdraw(1, 9);
    }
}
