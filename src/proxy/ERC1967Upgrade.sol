// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title ERC1967Upgrade — minimal ERC-1967 implementation slot logic.
/// @notice Self-contained (no OpenZeppelin dependency) so the project builds
///         offline once solc is cached. The implementation slot is the
///         canonical EIP-1967 value:
///         bytes32(uint256(keccak256("eip1967.proxy.implementation")) - 1)
abstract contract ERC1967Upgrade {
    bytes32 internal constant _IMPLEMENTATION_SLOT =
        0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc;

    event Upgraded(address indexed implementation);

    function _getImplementation() internal view returns (address impl) {
        // solhint-disable-next-line no-inline-assembly
        assembly {
            impl := sload(_IMPLEMENTATION_SLOT)
        }
    }

    function _setImplementation(address newImplementation) internal {
        require(
            newImplementation.code.length > 0,
            "ERC1967: new implementation is not a contract"
        );
        // solhint-disable-next-line no-inline-assembly
        assembly {
            sstore(_IMPLEMENTATION_SLOT, newImplementation)
        }
        emit Upgraded(newImplementation);
    }
}
