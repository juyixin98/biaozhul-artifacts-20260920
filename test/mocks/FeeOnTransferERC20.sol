// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {MockERC20} from "../../src/MockERC20.sol";

/// @title FeeOnTransferERC20
/// @notice Token that burns a fee on every transfer. Test-only: proves the
///         vault credits deposits by balance delta, not by the requested amount.
contract FeeOnTransferERC20 is MockERC20 {
    uint256 public constant FEE_BPS = 100; // 1%

    function _transfer(address from, address to, uint256 amount) internal override {
        uint256 fee = amount / 100; // 1%, floor
        uint256 received = amount - fee;
        uint256 fromBalance = balanceOf[from];
        require(fromBalance >= amount, "ERC20: insufficient balance");
        unchecked {
            balanceOf[from] = fromBalance - amount;
            totalSupply -= fee;
        }
        balanceOf[to] += received;
        emit Transfer(from, to, received);
        if (fee > 0) emit Transfer(from, address(0), fee);
    }
}
