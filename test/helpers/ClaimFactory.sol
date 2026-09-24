// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {MerkleClaim} from "../../src/MerkleClaim.sol";

/// @notice 测试用部署工厂：用普通 CREATE 部署 MerkleClaim。
///         CREATE 地址 = keccak256(rlp([factory, nonce]))[12:]，与构造参数（root）无关，
///         因此可以在部署前预测地址 → 先按该地址生成绑定叶子的 Merkle 根 → 再部署。
///         测试中用 vm.computeCreateAddress(address(factory), vm.getNonce(address(factory))) 预测。
contract ClaimFactory {
    event Deployed(address addr);

    function deploy(bytes32 root, address token_, address owner_) external returns (address addr) {
        MerkleClaim c = new MerkleClaim(root, token_, owner_);
        addr = payable(address(c));
        require(addr != address(0), "deploy failed");
        emit Deployed(addr);
    }
}
