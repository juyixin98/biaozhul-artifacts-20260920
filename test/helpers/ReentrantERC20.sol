// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @notice 带「接收回调」的 ERC-20：transfer 到合约收款方时回调 tokensReceived。
///         专门用于在测试中模拟恶意代币发起的重入（与标准 ERC-777 钩子同类）。
contract ReentrantERC20 {
    string public name = "Reentrant";
    string public symbol = "RNT";
    uint8 public constant decimals = 18;
    uint256 public totalSupply;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    constructor() {
        _mint(msg.sender, 1_000_000 ether);
    }

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _transfer(msg.sender, to, amount);
        _notify(to, msg.sender, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= amount, "insufficient allowance");
        if (allowed != type(uint256).max) allowance[from][msg.sender] = allowed - amount;
        _transfer(from, to, amount);
        _notify(to, from, amount);
        return true;
    }

    /// @dev 只对显式实现了接收钩子的合约回调：
    ///      - 收款方无此函数（如被测 HTLC，没有 fallback）：底层调用失败且 returndata 为空，静默跳过；
    ///      - 钩子存在但主动 revert（如重入撞互斥锁）：returndata 非空，必须原样向上抛出。
    function _notify(address to, address from, uint256 amount) internal {
        if (to.code.length == 0) return;
        bytes memory data = abi.encodeCall(IReceiver.tokensReceived, (from, amount));
        assembly {
            let ok := call(gas(), to, 0, add(data, 0x20), mload(data), 0, 0)
            if iszero(ok) {
                let size := returndatasize()
                if gt(size, 0) {
                    returndatacopy(0, 0, size)
                    revert(0, size)
                }
            }
        }
    }

    function _transfer(address from, address to, uint256 amount) internal {
        require(balanceOf[from] >= amount, "insufficient balance");
        unchecked {
            balanceOf[from] -= amount;
            balanceOf[to] += amount;
        }
    }

    function _mint(address to, uint256 amount) internal {
        totalSupply += amount;
        unchecked {
            balanceOf[to] += amount;
        }
    }
}

interface IReceiver {
    function tokensReceived(address from, uint256 amount) external;
}
