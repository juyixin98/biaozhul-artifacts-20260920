// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "../interfaces/IERC20.sol";

/// @notice 测试用 ERC20：公开 mint；可对指定接收地址启用 ERC777 风格转账回调
///         （tokensReceived(uint256)），用于模拟回调重入场景。
contract MockERC20 is IERC20 {
    string public name;
    string public symbol;
    uint8 public constant decimals = 18;

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    /// @dev 对哪些接收地址触发回调
    mapping(address => bool) public callbackEnabled;

    constructor(string memory _name, string memory _symbol) {
        name = _name;
        symbol = _symbol;
    }

    function mint(address to, uint256 amount) external {
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function setCallback(address who, bool on) external {
        callbackEnabled[who] = on;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _move(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= amount, "ERC20: insufficient allowance");
        if (allowed != type(uint256).max) allowance[from][msg.sender] = allowed - amount;
        _move(from, to, amount);
        return true;
    }

    function _move(address from, address to, uint256 amount) internal {
        require(balanceOf[from] >= amount, "ERC20: insufficient balance");
        unchecked {
            balanceOf[from] -= amount;
            balanceOf[to] += amount;
        }
        emit Transfer(from, to, amount);
        if (callbackEnabled[to] && to.code.length > 0) {
            // 冒泡回调中的 revert（含重入保护错误），模拟真实回调代币行为
            (bool ok, bytes memory data) = to.call(abi.encodeWithSignature("tokensReceived(uint256)", amount));
            if (!ok) {
                assembly {
                    revert(add(data, 32), mload(data))
                }
            }
        }
    }
}
