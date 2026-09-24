// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Vault} from "../src/Vault.sol";

/// @notice Self-contained smoke test (no forge-std dependency).
///         forge treats every non-reverting `test*` function as passing;
///         failed `require`s are the assertions.
contract VaultTest {
    address constant user = address(uint160(0xABCDEF));

    Vault vault;

    function setUp() public {
        vault = new Vault();
    }

    function testDepositUpdatesBalances() public {
        uint256 beforeDep = vault.totalDeposited();
        vault.deposit{value: 1 ether}();
        require(vault.balanceOf(address(this)) == 1 ether, "sender balance");
        require(vault.totalDeposited() == beforeDep + 1 ether, "total deposited");
    }

    function testWithdrawReducesBalance() public {
        vault.deposit{value: 2 ether}();
        vault.withdraw(500_000 gwei);
        require(vault.balanceOf(address(this)) == 1_999_500_000 gwei, "balance after withdraw");
        require(vault.totalWithdrawn() == 500_000 gwei, "total withdrawn");
    }

    function testWithdrawTooMuchReverts() public {
        vault.deposit{value: 1 wei}();
        bool reverted;
        try vault.withdraw(2 wei) {
            reverted = false;
        } catch {
            reverted = true;
        }
        require(reverted, "overdraw must revert");
    }

    // allow this test contract to receive withdrawn ETH
    receive() external payable {}
}
