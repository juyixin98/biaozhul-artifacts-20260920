// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {VestingStream} from "../../src/VestingStream.sol";
import {IERC20} from "openzeppelin-contracts/contracts/token/ERC20/IERC20.sol";

/// @notice 恶意参与者：在收到代币回调时尝试重入归属合约。
///         既可作为受益人（claim 回调重入），也可作为发送者
///         （cancel 退款回调重入）。重入结果记录在案，供测试断言；
///         回调本身正常返回，以便外层调用在重入被拒绝后仍能完成。
contract ReentrantClaimer {
    VestingStream public immutable vesting;
    uint256 public streamId;

    enum Attack {
        None,
        ReenterClaim,
        ReenterCancel,
        ReenterTopUp
    }

    Attack public nextAttack = Attack.None;
    bool public reentrySucceeded;
    bytes public reentryRevertReason;

    constructor(VestingStream _vesting) {
        vesting = _vesting;
    }

    function setStreamId(uint256 id) external {
        streamId = id;
    }

    function arm(Attack attack) external {
        nextAttack = attack;
        reentrySucceeded = false;
        delete reentryRevertReason;
    }

    function claim() external {
        vesting.claim(streamId);
    }

    /// @dev 作为 sender 使用：授权代币并代为创建流。
    function initToken(IERC20 token) external {
        token.approve(address(vesting), type(uint256).max);
    }

    function createStream(address beneficiary, uint128 amount, uint64 start, uint64 cliff, uint64 end)
        external
        returns (uint256)
    {
        return vesting.createStream(beneficiary, amount, start, cliff, end);
    }

    function cancel() external {
        vesting.cancel(streamId);
    }

    function onTokenReceived(address, uint256) external {
        Attack attack = nextAttack;
        nextAttack = Attack.None; // 只攻击一次，避免无限递归
        if (attack == Attack.ReenterClaim) {
            try vesting.claim(streamId) {
                reentrySucceeded = true;
            } catch (bytes memory reason) {
                reentryRevertReason = reason;
            }
        } else if (attack == Attack.ReenterCancel) {
            try vesting.cancel(streamId) {
                reentrySucceeded = true;
            } catch (bytes memory reason) {
                reentryRevertReason = reason;
            }
        } else if (attack == Attack.ReenterTopUp) {
            try vesting.topUp(streamId, 1) {
                reentrySucceeded = true;
            } catch (bytes memory reason) {
                reentryRevertReason = reason;
            }
        }
    }
}
