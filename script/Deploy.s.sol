// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {MockERC20} from "../src/MockERC20.sol";
import {ShareVault} from "../src/ShareVault.sol";

/// @notice 本地部署脚本：先部署模拟资产，再部署金库。
/// @dev 用法：
///   anvil（另开终端）
///   forge script script/Deploy.s.sol --rpc-url anvil --broadcast --private-key 0xac0974...
contract Deploy is Script {
    function run() external returns (MockERC20 token, ShareVault vault) {
        uint256 pk = vm.envOr("DEPLOYER_PK", uint256(0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80));
        vm.startBroadcast(pk);
        token = new MockERC20("Mock Token", "MOCK", 18);
        vault = new ShareVault(address(token), "Mock Share Vault", "sMOCK");
        vm.stopBroadcast();
        console2.log("MockERC20:", address(token));
        console2.log("ShareVault:", address(vault));
    }
}
