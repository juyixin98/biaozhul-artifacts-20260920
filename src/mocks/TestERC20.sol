// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {ERC20} from "../ERC20.sol";

/// @notice Plain test asset, freely mintable. No transfer fee.
contract TestERC20 is ERC20 {
    constructor(string memory _name, string memory _symbol) ERC20(_name, _symbol) {}

    function mint(address to, uint256 value) external {
        _mint(to, value);
    }
}
