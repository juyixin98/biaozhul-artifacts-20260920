// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "./IERC20.sol";

/// @title 本地测试资产（可公开铸造的极简 ERC-20）
/// @notice 仅用于本地双链演示，任何人都可以铸造，没有任何真实价值。
/// @dev    用 keccak256(abi.encodePacked(name)) 派生 EIP-712 不需要的简单代币。
contract TestToken is IERC20 {
    string public name;
    string public symbol;
    uint8 public constant decimals = 18;
    uint256 public totalSupply;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Mint(address indexed to, uint256 value);

    constructor(string memory _name, string memory _symbol) {
        name = _name;
        symbol = _symbol;
    }

    /// @notice 演示用：任意账户可自由铸造测试代币。
    function mint(address to, uint256 value) external {
        require(to != address(0), "TestToken: mint to zero address");
        totalSupply += value;
        balanceOf[to] += value;
        emit Mint(to, value);
        emit Transfer(address(0), to, value);
    }

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transfer(address to, uint256 value) external returns (bool) {
        _transfer(msg.sender, to, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= value, "TestToken: insufficient allowance");
        if (allowed != type(uint256).max) {
            allowance[from][msg.sender] = allowed - value;
        }
        _transfer(from, to, value);
        return true;
    }

    function _transfer(address from, address to, uint256 value) internal {
        require(to != address(0), "TestToken: transfer to zero address");
        require(balanceOf[from] >= value, "TestToken: insufficient balance");
        unchecked {
            balanceOf[from] -= value;
        }
        balanceOf[to] += value;
        emit Transfer(from, to, value);
    }
}
