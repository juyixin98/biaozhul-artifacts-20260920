// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @title Ecdsa
/// @notice Hand-rolled ECDSA recovery for secp256k1: EIP-2 malleability checks,
///         EIP-2098 64-byte compact form and the usual 65-byte r,s,v form.
///         No third-party code is used; only the EVM `ecrecover` precompile.
library Ecdsa {
    /// @dev ecrecover precompile returned address(0) or s/v are out of range.
    error InvalidSignature();

    /// @notice Recover the signer of `hash` from a 64- or 65-byte signature.
    function recover(bytes32 hash, bytes memory signature) internal pure returns (address signer) {
        bool success;
        (signer, success) = tryRecover(hash, signature);
        if (!success) revert InvalidSignature();
    }

    /// @notice Like {recover}, but returns a success flag instead of reverting.
    function tryRecover(bytes32 hash, bytes memory signature)
        internal
        pure
        returns (address signer, bool success)
    {
        if (signature.length == 65) {
            bytes32 r;
            bytes32 s;
            uint8 v;
            // solhint-disable-next-line no-inline-assembly
            assembly ("memory-safe") {
                r := mload(add(signature, 0x20))
                s := mload(add(signature, 0x40))
                v := byte(0, mload(add(signature, 0x60)))
            }
            return tryRecover(hash, v, r, s);
        } else if (signature.length == 64) {
            bytes32 r;
            bytes32 vs;
            // solhint-disable-next-line no-inline-assembly
            assembly ("memory-safe") {
                r := mload(add(signature, 0x20))
                vs := mload(add(signature, 0x40))
            }
            return recoverCompressed(hash, r, vs);
        } else {
            return (address(0), false);
        }
    }

    /// @notice Recover from an EIP-2098 compact signature (r, vs).
    function recoverCompressed(bytes32 hash, bytes32 r, bytes32 vs)
        internal
        pure
        returns (address signer, bool success)
    {
        // EIP-2: the high bit of vs carries the parity bit; s is the low 255 bits.
        unchecked {
            bytes32 s =
                vs & bytes32(0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff);
            uint8 v = uint8((uint256(vs) >> 255) + 27);
            return tryRecover(hash, v, r, s);
        }
    }

    /// @notice Recover from raw recovery parameters.
    function tryRecover(bytes32 hash, uint8 v, bytes32 r, bytes32 s)
        internal
        pure
        returns (address signer, bool success)
    {
        // EIP-2: only lower-half s values are canonical (no signature malleability).
        if (uint256(s) > 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0) {
            return (address(0), false);
        }
        if (v != 27 && v != 28) {
            if (v == 0 || v == 1) {
                v += 27;
            } else {
                return (address(0), false);
            }
        }
        signer = ecrecover(hash, v, r, s);
        return (signer, signer != address(0));
    }

    /// @notice Recover from raw recovery parameters, reverting on failure.
    function recover(bytes32 hash, uint8 v, bytes32 r, bytes32 s) internal pure returns (address) {
        (address signer, bool success) = tryRecover(hash, v, r, s);
        if (!success) revert InvalidSignature();
        return signer;
    }
}
