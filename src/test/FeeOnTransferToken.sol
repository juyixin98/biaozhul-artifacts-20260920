// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "../interfaces/IERC20.sol";

/// @title FeeOnTransferToken
/// @notice Test asset that deducts a configurable fee on EVERY transfer, paid
///         at the recipient's side: recipient receives (1 - feeBps) of the
///         nominal amount; the fee is burned. Used to prove the pool refuses
///         unsupported deflationary tokens (the strict balance-delta checks
///         revert on both deposit and swap entry paths).
contract FeeOnTransferToken is IERC20 {
    string public name;
    string public symbol;
    uint8 public constant decimals = 18;

    /// @dev Fee in basis points (e.g. 100 = 1%).
    uint256 public immutable feeBps;

    /// @dev Switchable so a test can seed an honest pool and then enable the
    ///      fee, exercising the pool's strict delta checks on every later
    ///      entry path (a fee token cannot be part of a real pool at all).
    bool public feeOn;

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    constructor(string memory _name, string memory _symbol, uint256 _feeBps) {
        name = _name;
        symbol = _symbol;
        feeBps = _feeBps;
        feeOn = true;
    }

    function setFeeOn(bool on) external {
        feeOn = on;
    }

    function mint(address to, uint256 amount) external {
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
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
        if (allowed != type(uint256).max) {
            require(allowed >= value, "FOT: allowance");
            unchecked {
                allowance[from][msg.sender] = allowed - value;
            }
        }
        _transfer(from, to, value);
        return true;
    }

    function _transfer(address from, address to, uint256 value) private {
        require(balanceOf[from] >= value, "FOT: balance");
        uint256 fee = feeOn ? (value * feeBps) / 10_000 : 0;
        unchecked {
            balanceOf[from] -= value;
            balanceOf[to] += value - fee;
            totalSupply -= fee;
        }
        emit Transfer(from, to, value - fee);
    }
}
