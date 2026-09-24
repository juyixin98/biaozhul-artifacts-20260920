// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC1967Upgrade} from "./ERC1967Upgrade.sol";

/// @title ERC1967Proxy — minimal transparent-style ERC-1967 UUPS proxy.
/// @notice There is no separate admin slot: UUPS upgrades are authorised by
///         the *logic* contract (see UUPSUpgradable). Anyone calling
///         `upgradeTo` on the proxy simply gets delegated to the logic, which
///         checks `_owner`.
contract ERC1967Proxy is ERC1967Upgrade {
    /// @param implementation initial logic contract
    /// @param data calldata forwarded via delegatecall (e.g. an init call);
    ///        use empty bytes for no initializer
    constructor(address implementation, bytes memory data) payable {
        _setImplementation(implementation);
        if (data.length > 0) {
            // solhint-disable-next-line avoid-low-level-calls
            (bool ok, bytes memory ret) = implementation.delegatecall(data);
            require(ok, _bubble(ret));
        }
    }

    /// @dev Fallback: delegate every call to the current implementation.
    // solhint-disable-next-line no-complex-fallback
    fallback() external payable {
        address impl = _getImplementation();
        // solhint-disable-next-line no-inline-assembly
        assembly {
            calldatacopy(0, 0, calldatasize())
            let result := delegatecall(gas(), impl, 0, calldatasize(), 0, 0)
            returndatacopy(0, 0, returndatasize())
            switch result
            case 0 {
                revert(0, returndatasize())
            }
            default {
                return(0, returndatasize())
            }
        }
    }

    receive() external payable {}

    function _bubble(bytes memory ret) private pure returns (string memory) {
        if (ret.length > 0) {
            // solhint-disable-next-line no-inline-assembly
            assembly {
                revert(add(ret, 0x20), mload(ret))
            }
        }
        return "ERC1967Proxy: init failed";
    }
}
