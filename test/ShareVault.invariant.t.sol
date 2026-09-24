// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {MockERC20} from "../src/MockERC20.sol";
import {ShareVault} from "../src/ShareVault.sol";
import {Handler} from "./handlers/Handler.sol";

/// @notice 状态模糊测试：随机序列地存款/赎回/捐赠/转账后，持续校验会计不变量。
contract ShareVaultInvariantTest is Test {
    MockERC20 token;
    ShareVault vault;
    Handler handler;

    address[] actors;

    uint256 constant DEAD_SHARES = 1000;

    function setUp() public {
        token = new MockERC20("Mock Token", "MOCK", 18);
        vault = new ShareVault(address(token), "Mock Share Vault", "sMOCK");

        for (uint256 i = 0; i < 4; i++) {
            actors.push(address(uint160(uint256(keccak256(abi.encodePacked("actor", i))))));
        }
        handler = new Handler(token, vault, actors);

        // 只允许 Handler 的四个真实动作，避免回退/自销毁等噪音。
        bytes4[] memory selectors = new bytes4[](4);
        selectors[0] = Handler.deposit.selector;
        selectors[1] = Handler.redeem.selector;
        selectors[2] = Handler.donate.selector;
        selectors[3] = Handler.transferShares.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: selectors}));
        targetContract(address(handler));
    }

    /// @dev 不变量 1：初始化前任何直接捐赠都不会铸造份额；初始化后总份额永远
    ///      >= 死份额，死份额余额恒为 1000，且资产永远 >= 死份额背书。
    function invariant_deadSharesPermanentlyLocked() public view {
        if (vault.totalSupply() == 0) {
            // 允许空库持有捐赠资产（设计如此：捐赠不铸份额），但份额必须仍为 0。
            assertEq(vault.balanceOf(address(1)), 0, "no dead shares before init");
            return;
        }
        assertGe(vault.totalSupply(), DEAD_SHARES, "supply never drops below dead shares");
        assertEq(vault.balanceOf(address(1)), DEAD_SHARES, "dead balance constant");
        assertGe(vault.totalAssets(), DEAD_SHARES, "assets back dead shares");
    }

    /// @dev 不变量 2：集体偿付能力——所有活份额（参与者）+ 死份额按当前汇率
    ///      可兑资产之和不超过库内资产（每人允许 1 wei 向下取整误差）。
    ///      这正是“资产份额关系”的核心：sum_i floor(s_i*A/T) <= A。
    function invariant_collectiveSolvency() public view {
        uint256 supply = vault.totalSupply();
        if (supply == 0) return;
        uint256 assets = vault.totalAssets();

        uint256 claimable;
        uint256 counted;
        for (uint256 i = 0; i < actors.length; i++) {
            uint256 bal = vault.balanceOf(actors[i]);
            counted += bal;
            claimable += (bal * assets) / supply;
        }
        counted += DEAD_SHARES;
        claimable += (DEAD_SHARES * assets) / supply;

        // 参与集之外可能还有零余额地址；断言覆盖的份额 <= 总份额。
        assertLe(counted, supply, "tracked shares cannot exceed supply");
        // 向下取整保证可兑总量不超过库产。
        assertLe(claimable, assets, "vault always solvent against all shares");
    }

    /// @dev 不变量 3：汇率（A/T）单调不减——存款不摊薄旧份额，
    ///      捐赠只会抬高或维持汇率，向下取整的赎回也不摊薄剩余份额。
    function invariant_rateNeverDecreases() public view {
        uint256 supply = vault.totalSupply();
        if (supply == 0) return;
        uint256 current = (vault.totalAssets() * 1e18) / supply;
        uint256 min = handler.ghost_lastRateX1e18();
        if (min != 0) {
            assertGe(current, min, "exchange rate never decreases");
        }
    }

    /// @dev 不变量 4：铸造/销毁守恒——总供给 = 累计铸造 - 累计销毁
    ///      （注意首次铸造还包含 1000 死份额）。
    function invariant_supplyConservation() public view {
        uint256 supply = vault.totalSupply();
        uint256 minted = handler.ghost_mintedShares();
        uint256 burned = handler.ghost_burnedShares();
        if (minted == 0) {
            assertEq(supply, 0);
        } else {
            // 用户累计铸造份额 + 1000 死份额 - 累计销毁 = 当前供给
            assertEq(supply + burned, minted + DEAD_SHARES, "share mint/burn conservation");
        }
    }
}
