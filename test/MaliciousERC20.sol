// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @notice 恶意 ERC20：在 transfer / transferFrom 中回调调用方（金库），
///         模拟不可信代币的重入攻击。正常流程不应依赖资产代币“老实”。
contract MaliciousERC20 {
    string public name = "Malicious Token";
    string public symbol = "EVIL";
    uint8 public constant decimals = 18;
    uint256 public totalSupply;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    /// @notice 回调目标与 calldata。非空时，在记账完成后、返回收款方前执行。
    address public hookTarget;
    bytes public hookData;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor(uint256 initialMint) {
        balanceOf[msg.sender] = initialMint;
        totalSupply = initialMint;
        emit Transfer(address(0), msg.sender, initialMint);
    }

    function setHook(address target, bytes calldata data) external {
        hookTarget = target;
        hookData = data;
    }

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transfer(address to, uint256 value) external returns (bool) {
        _transfer(msg.sender, to, value);
        _hook();
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        if (allowance[from][msg.sender] != type(uint256).max) {
            allowance[from][msg.sender] -= value;
        }
        _transfer(from, to, value);
        _hook();
        return true;
    }

    /// @notice 攻击助手：合约自身作为攻击主体，需要持有本币并已授权金库。
    function _transfer(address from, address to, uint256 value) internal {
        balanceOf[from] -= value;
        balanceOf[to] += value;
        emit Transfer(from, to, value);
    }

    function _hook() internal {
        address t = hookTarget;
        if (t != address(0) && hookData.length != 0) {
            (bool ok,) = t.call(hookData);
            // 静默吞掉回滚，方便测试同时观察：重入调用即使 revert，主调用也必须安全。
            ok;
        }
    }
}
