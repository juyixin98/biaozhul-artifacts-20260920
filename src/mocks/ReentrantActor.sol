// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {StreamVesting} from "../StreamVesting.sol";
import {IERC20} from "../interfaces/IERC20.sol";

/// @notice 重入攻击者：既可作为 sender（撤销退款时回调），也可作为 beneficiary
///         （领取转账时回调）。收到代币回调时按预设模式重入归属合约。
contract ReentrantActor {
    enum Attack {
        None,
        Withdraw,
        Cancel
    }

    StreamVesting public immutable vesting;
    IERC20 public immutable token;

    Attack public attack;
    uint256 public streamId;

    constructor(StreamVesting _vesting, IERC20 _token) {
        vesting = _vesting;
        token = _token;
    }

    function setAttack(Attack _attack, uint256 _streamId) external {
        attack = _attack;
        streamId = _streamId;
    }

    /// @dev MockERC20 的转账回调钩子
    function tokensReceived(uint256) external {
        Attack a = attack;
        if (a == Attack.Withdraw) vesting.withdraw(streamId);
        else if (a == Attack.Cancel) vesting.cancel(streamId);
    }

    // --- 代理操作，便于测试通过该合约发起调用 ---

    function createStream(address beneficiary, uint128 amount, uint64 start, uint64 cliff, uint64 end)
        external
        returns (uint256)
    {
        token.approve(address(vesting), amount);
        return vesting.createStream(beneficiary, amount, start, cliff, end);
    }

    function withdraw(uint256 id) external {
        vesting.withdraw(id);
    }

    function cancel(uint256 id) external {
        vesting.cancel(id);
    }
}
