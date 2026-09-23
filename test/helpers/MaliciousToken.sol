// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "../../src/IERC20.sol";

/// @notice 重入测试专用的恶意 ERC-20：
///         向 attacker 转账时回调 attacker，给 HashTimeLock.claim 制造重入窗口。
contract MaliciousToken is IERC20 {
    string public constant name = "Malicious";
    string public constant symbol = "MAL";
    uint8 public constant decimals = 18;
    uint256 public totalSupply;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    /// @notice 下一次“发给该地址”的转账要回调的合约（攻击合约地址）。
    mapping(address => bool) public hooks;

    function mint(address to, uint256 value) external {
        totalSupply += value;
        balanceOf[to] += value;
        emit Transfer(address(0), to, value);
    }

    /// @notice 注册：凡是向 hook 地址转账，就调用其 onTokenReceived。
    function setHook(address hook, bool enabled) external {
        hooks[hook] = enabled;
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
        require(allowed >= value, "MAL: allowance");
        if (allowed != type(uint256).max) {
            allowance[from][msg.sender] = allowed - value;
        }
        _transfer(from, to, value);
        return true;
    }

    function _transfer(address from, address to, uint256 value) internal {
        require(balanceOf[from] >= value, "MAL: balance");
        unchecked {
            balanceOf[from] -= value;
        }
        balanceOf[to] += value;
        emit Transfer(from, to, value);
        if (hooks[to]) {
            // 把调用参数透传给攻击合约，攻击合约在回调里尝试重入 claim。
            IAttackHook(to).onTokenReceived(from, value);
        }
    }
}

interface IAttackHook {
    function onTokenReceived(address from, uint256 value) external;
}
