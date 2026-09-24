// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Script, console} from "forge-std/Script.sol";
import {MerkleClaim} from "../src/MerkleClaim.sol";
import {TestMerkle} from "../test/TestMerkle.sol";

/// @notice 参考部署脚本（生产中由 Python scripts/deploy.py 完成，因为分配数据在后端）。
///         这里用固定的示例参数演示"先预测地址、再造根、再部署"的顺序。
///         用法：
///           RPC_URL=http://127.0.0.1:8545 \
///           PRIVATE_KEY=0xac0974... forge script script/Deploy.s.sol --rpc-url $RPC_URL --broadcast
contract Deploy is Script {
    function run() external returns (MerkleClaim deployed) {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        address deployer = vm.addr(pk);

        // 示例：单个领取者（index 0）。真实部署请使用 Python 脚本读取 allocations.json。
        address recipient = vm.envOr("RECIPIENT", address(0x70997970C51812dc3A010C7d01b50e0d17dc79C8));
        uint256 amount = vm.envOr("AMOUNT", uint256(1 ether));

        vm.startBroadcast(pk);

        // 1) 预测本合约地址（CREATE 使用当前 nonce）
        address predicted = vm.computeCreateAddress(deployer, vm.getNonce(deployer));
        // 2) 用预测地址构造单叶树（root == leaf，证明为空）
        bytes32 leaf = TestMerkle.hashLeaf(block.chainid, predicted, 0, recipient, amount);
        bytes32[] memory leaves = new bytes32[](1);
        leaves[0] = leaf;
        bytes32 root = TestMerkle.getRoot(leaves);
        // 3) 携带资金部署
        deployed = new MerkleClaim{value: amount}(root);

        vm.stopBroadcast();

        require(address(deployed) == predicted, "address mismatch");
        console.log("MerkleClaim deployed at", address(deployed));
        console.log("root");
        console.logBytes32(root);
    }
}
