// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "./interfaces/IERC20.sol";

/// @title MockERC20
/// @notice Simulated asset used for local testing ONLY. Anyone can mint, which
/// is exactly what we want on an Anvil chain and what must never ship to a real
/// network. Fixed 18 decimals, no fees, fully standard ERC-20 return values.
contract MockERC20 is IERC20 {
    string public constant name = "Mock Asset";
    string public constant symbol = "MOCK";
    uint8 public constant decimals = 18;

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    /// @dev Override in a subclass to simulate malicious transfer callbacks.
    /// The default token is a well-behaved no-op hook.
    function _afterTokenOperation(address from, address to, uint256 amount) internal virtual {}

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }

    function burn(address from, uint256 amount) external {
        _spendAllowance(from, msg.sender, amount);
        _burn(from, amount);
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _transfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        _spendAllowance(from, msg.sender, amount);
        _transfer(from, to, amount);
        return true;
    }

    function _transfer(address from, address to, uint256 amount) internal virtual {
        uint256 fromBalance = balanceOf[from];
        require(fromBalance >= amount, "ERC20: insufficient balance");
        unchecked {
            balanceOf[from] = fromBalance - amount;
        }
        balanceOf[to] += amount;
        emit Transfer(from, to, amount);
        _afterTokenOperation(from, to, amount);
    }

    function _mint(address to, uint256 amount) internal {
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
        _afterTokenOperation(address(0), to, amount);
    }

    function _burn(address from, uint256 amount) internal {
        uint256 fromBalance = balanceOf[from];
        require(fromBalance >= amount, "ERC20: burn exceeds balance");
        unchecked {
            balanceOf[from] = fromBalance - amount;
        }
        totalSupply -= amount;
        emit Transfer(from, address(0), amount);
    }

    function _spendAllowance(address owner, address spender, uint256 amount) internal {
        uint256 current = allowance[owner][spender];
        if (current != type(uint256).max) {
            require(current >= amount, "ERC20: insufficient allowance");
            unchecked {
                allowance[owner][spender] = current - amount;
            }
        }
    }
}
