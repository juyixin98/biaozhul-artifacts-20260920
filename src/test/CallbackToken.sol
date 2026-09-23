// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "../interfaces/IERC20.sol";

interface ICallbackReceiver {
    /// @notice Called by CallbackToken whenever the attacker is the sender or
    ///         recipient of a transfer (ERC-777-style hook).
    function cpmmTokenCallback() external;
}

/// @title CallbackToken
/// @notice Otherwise-honest ERC-20 (exact balance deltas) that invokes a
///         registered receiver hook on every transfer touching it. The hook
///         lets a malicious recipient/sender attempt to reenter the pool
///         while its transfer is mid-flight.
contract CallbackToken is IERC20 {
    string public name;
    string public symbol;
    uint8 public constant decimals = 18;

    address public immutable hookTarget;

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    constructor(string memory _name, string memory _symbol, address _hookTarget) {
        name = _name;
        symbol = _symbol;
        hookTarget = _hookTarget;
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
            require(allowed >= value, "CB: allowance");
            unchecked {
                allowance[from][msg.sender] = allowed - value;
            }
        }
        _transfer(from, to, value);
        return true;
    }

    function _transfer(address from, address to, uint256 value) private {
        require(balanceOf[from] >= value, "CB: balance");
        unchecked {
            balanceOf[from] -= value;
            balanceOf[to] += value;
        }
        emit Transfer(from, to, value);

        if (from == hookTarget || to == hookTarget) {
            // Reentrant call into the caller, executed while the outer pool
            // operation still holds its lock.
            ICallbackReceiver(hookTarget).cpmmTokenCallback();
        }
    }
}
