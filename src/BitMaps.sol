// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title  BitMaps
/// @notice 自包含实现（不依赖 OpenZeppelin），用一个 uint256 打包 256 个领取位。
///         位图按"索引"置位，是防重复领取的核心。
library BitMaps {
    struct BitMap {
        mapping(uint256 bucket => uint256 bits) _data;
    }

    /// @dev 索引所在的 uint256 桶编号
    function bucketOf(uint256 index) internal pure returns (uint256) {
        return index >> 8;
    }

    /// @dev 索引在桶内的位掩码
    function maskOf(uint256 index) internal pure returns (uint256) {
        return uint256(1) << (index & 0xff);
    }

    /// @notice 该索引是否已经领取
    function get(BitMap storage bitmap, uint256 index) internal view returns (bool) {
        return (bitmap._data[bucketOf(index)] & maskOf(index)) != 0;
    }

    /// @notice 置位；若已置位则回滚整笔交易（调用方应先检查，这里做防御性保证）
    function set(BitMap storage bitmap, uint256 index) internal {
        uint256 bucket = bucketOf(index);
        uint256 mask = maskOf(index);
        require((bitmap._data[bucket] & mask) == 0, "BitMap: already set");
        bitmap._data[bucket] |= mask;
    }
}
