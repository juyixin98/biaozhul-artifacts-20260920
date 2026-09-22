// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {MockERC20} from "./MockERC20.sol";

interface ITokenCallback {
    /// @notice 代币转账回调（模拟 ERC777/ERC1363 风格的接收钩子）。
    function onTokenReceived(address from, uint256 amount) external;
}

/// @notice 带转账回调的 ERC20：当武装(armed)且收款方是 hook 目标时，
///         在转账过程中回调收款方，用于测试归属合约的重入防护。
contract CallbackToken is MockERC20 {
    bool public armed;

    constructor() MockERC20("Callback Token", "CBT") {}

    function setArmed(bool _armed) external {
        armed = _armed;
    }

    function _update(address from, address to, uint256 value) internal override {
        super._update(from, to, value);
        // mint(from==0) 不回调；仅在武装且收款方能接收回调时触发
        if (armed && from != address(0) && to.code.length > 0) {
            // 若目标未实现回调接口则静默跳过（try/catch 不吞掉目标主动 revert）
            try ITokenCallback(to).onTokenReceived(from, value) {}
            catch (bytes memory reason) {
                // 目标显式 revert 时向上传播，保证转账原子回滚
                if (reason.length > 0) {
                    assembly {
                        revert(add(reason, 32), mload(reason))
                    }
                }
            }
        }
    }
}
