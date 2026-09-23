// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {ERC20} from "../ERC20.sol";

/// @notice Token that deducts `feeBps` basis points on every transfer.
///         Used to prove the pool rejects fee-on-transfer assets.
contract FeeOnTransferERC20 is ERC20 {
    uint256 public immutable feeBps;

    constructor(uint256 _feeBps) ERC20("Fee Token", "FEE") {
        feeBps = _feeBps; // e.g. 30 = 0.30%
    }

    function mint(address to, uint256 value) external {
        _mint(to, value);
    }

    function _transfer(address from, address to, uint256 value) internal override {
        uint256 fee = (value * feeBps) / 10_000;
        require(balanceOf[from] >= value, "ERC20: balance");
        unchecked {
            balanceOf[from] -= value;
        }
        balanceOf[to] += value - fee;
        // Fee is burned (sender already debited full value).
        totalSupply -= fee;
        emit Transfer(from, to, value - fee);
    }
}
