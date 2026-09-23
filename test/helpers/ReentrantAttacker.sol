// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IAttackHook} from "./MaliciousToken.sol";

interface IHashTimeLock {
    function claim(uint256 lockId, bytes calldata secret) external;
}

/// @notice 重入攻击合约：收到恶意代币转账（即 HTLC 在 claim 末尾给它打钱）时，
///         立刻再次调用同一个 claim，试图在状态清理前重复领取。
contract ReentrantAttacker is IAttackHook {
    IHashTimeLock public immutable htlc;
    bytes public secret;
    uint256 public lockId;
    uint256 public reentrySuccesses;

    constructor(address _htlc) {
        htlc = IHashTimeLock(_htlc);
    }

    function arm(uint256 _lockId, bytes calldata _secret) external {
        lockId = _lockId;
        secret = _secret;
    }

    function onTokenReceived(address, uint256) external override {
        // 回调发生在“外层 claim 已置 CLAIMED、正在转账”的过程中。
        // 重入应被 nonReentrant 拦截（ReentrantCall）。
        try htlc.claim(lockId, secret) {
            reentrySuccesses += 1; // 绝不应该发生
        } catch {
            // 预期：重入被拒。
        }
    }
}
