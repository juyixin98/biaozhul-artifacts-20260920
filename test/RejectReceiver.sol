// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice 拒绝接收任何 ETH 的账户，用于触发 claimBatch 中转账失败路径。
contract RejectReceiver {
    error NoEthPlease();

    receive() external payable {
        revert NoEthPlease();
    }
}
