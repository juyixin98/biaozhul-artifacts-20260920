// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "../interfaces/IERC20.sol";

/// @title NoReturnToken
/// @notice Honest ERC-20 that returns NO data from transfer/transferFrom
///         (USDT-style). The pool's low-level call handling must accept it.
contract NoReturnToken {
    string public name;
    string public symbol;
    uint8 public constant decimals = 18;

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    constructor(string memory _name, string memory _symbol) {
        name = _name;
        symbol = _symbol;
    }

    function mint(address to, uint256 amount) external {
        totalSupply += amount;
        balanceOf[to] += amount;
        emit IERC20.Transfer(address(0), to, amount);
    }

    function approve(address spender, uint256 value) external {
        allowance[msg.sender][spender] = value;
        emit IERC20.Approval(msg.sender, spender, value);
    }

    function transfer(address to, uint256 value) external {
        require(balanceOf[msg.sender] >= value, "NRT: balance");
        balanceOf[msg.sender] -= value;
        balanceOf[to] += value;
        emit IERC20.Transfer(msg.sender, to, value);
    }

    function transferFrom(address from, address to, uint256 value) external {
        uint256 allowed = allowance[from][msg.sender];
        if (allowed != type(uint256).max) {
            require(allowed >= value, "NRT: allowance");
            unchecked {
                allowance[from][msg.sender] = allowed - value;
            }
        }
        require(balanceOf[from] >= value, "NRT: balance");
        balanceOf[from] -= value;
        balanceOf[to] += value;
        emit IERC20.Transfer(from, to, value);
    }
}
