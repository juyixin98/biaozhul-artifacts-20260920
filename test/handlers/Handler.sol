// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {MockERC20} from "../../src/MockERC20.sol";
import {ShareVault} from "../../src/ShareVault.sol";

/// @notice 状态模糊测试的受限动作集：存款 / 赎回 / 直接捐赠 / 份额转账。
/// @dev 所有外部调用都经 vm.prank 以真实参与者身份发起；金额经 bound 约束到合法区间，
///      避免“必然回滚”的无效用例。ghost 变量供不变量断言使用。
contract Handler is Test {
    MockERC20 public token;
    ShareVault public vault;

    address[] public actors;

    // ghost 计数器
    uint256 public ghost_depositCount;
    uint256 public ghost_redeemCount;
    uint256 public ghost_donateCount;
    uint256 public ghost_mintedShares;
    uint256 public ghost_burnedShares;
    uint256 public ghost_assetsIn;
    uint256 public ghost_assetsOut;
    /// @dev 上一次操作后的汇率（assets per share，放大 1e18），0 表示尚未初始化。
    uint256 public ghost_lastRateX1e18;

    constructor(MockERC20 token_, ShareVault vault_, address[] memory actors_) {
        token = token_;
        vault = vault_;
        for (uint256 i = 0; i < actors_.length; i++) {
            actors.push(actors_[i]);
            deal(address(token), actors_[i], 1e28);
            vm.prank(actors_[i]);
            token.approve(address(vault), type(uint256).max);
        }
    }

    function actorsLength() external view returns (uint256) {
        return actors.length;
    }

    function deposit(uint256 actorSeed, uint256 amount) external {
        address who = actors[actorSeed % actors.length];
        uint256 supply = vault.totalSupply();

        // 首次存款必须 > 1000；之后保证金额足够大，避免必然的 ZeroShares。
        uint256 minAssets;
        if (supply == 0) {
            minAssets = 1001;
        } else {
            minAssets = vault.totalAssets() / supply + 1;
        }
        amount = bound(amount, minAssets, minAssets + 1e24);

        uint256 balBefore = token.balanceOf(who);
        if (balBefore < amount) return; // 资金不足则跳过（理论上不会发生）

        vm.prank(who);
        uint256 shares = vault.deposit(amount, who, 0);

        ghost_depositCount++;
        ghost_mintedShares += shares;
        ghost_assetsIn += amount;
        _snapshotRate();
    }

    function redeem(uint256 actorSeed, uint256 shareAmt) external {
        address who = actors[actorSeed % actors.length];
        uint256 bal = vault.balanceOf(who);
        if (bal == 0) return;
        shareAmt = bound(shareAmt, 1, bal);

        vm.prank(who);
        uint256 out = vault.redeem(shareAmt, who, who, 0);

        ghost_redeemCount++;
        ghost_burnedShares += shareAmt;
        ghost_assetsOut += out;
        _snapshotRate();
    }

    /// @notice 直接向金库转资产（经典“捐赠”），不铸造任何份额。
    function donate(uint256 actorSeed, uint256 amount) external {
        address who = actors[actorSeed % actors.length];
        amount = bound(amount, 0, 1e22);
        if (token.balanceOf(who) < amount) return;
        vm.prank(who);
        token.transfer(address(vault), amount);
        ghost_donateCount++;
        _snapshotRate();
    }

    /// @notice 份额在参与者之间转移（份额代币的普通 ERC20 行为）。
    function transferShares(uint256 fromSeed, uint256 toSeed, uint256 shareAmt) external {
        address from = actors[fromSeed % actors.length];
        address to = actors[toSeed % actors.length];
        if (from == to) return;
        uint256 bal = vault.balanceOf(from);
        if (bal == 0) return;
        shareAmt = bound(shareAmt, 1, bal);
        vm.prank(from);
        vault.transfer(to, shareAmt);
        _snapshotRate();
    }

    function _snapshotRate() internal {
        uint256 supply = vault.totalSupply();
        if (supply == 0) return;
        uint256 rate = (vault.totalAssets() * 1e18) / supply;
        // 记录历史最低汇率：断言里检查它不被突破。
        if (ghost_lastRateX1e18 == 0 || rate < ghost_lastRateX1e18) {
            ghost_lastRateX1e18 = rate;
        }
    }
}
