// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-vm/Test.sol";
import {Ecdsa} from "../src/Ecdsa.sol";

/// @notice Direct unit tests for the hand-rolled ECDSA library. Every signature
///         here is a REAL secp256k1 signature produced by vm.sign; recovery
///         runs through the EVM ecrecover precompile.
contract EcdsaTest is Test {
    uint256 internal constant PK = 0xA11CE;
    address internal signer;

    // secp256k1 group order n.
    uint256 internal constant N =
        0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;

    function setUp() public {
        signer = vm.addr(PK);
    }

    function test_Recover_RSV_MatchesSigner() public {
        bytes32 h = keccak256("hello");
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(PK, h);
        bytes memory sig = abi.encodePacked(r, s, v);
        assertEq(Ecdsa.recover(h, sig), signer);
    }

    function test_Recover_Compact64Byte_MatchesSigner() public {
        bytes32 h = keccak256("compact");
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(PK, h);
        // EIP-2098: pack parity into the top bit of vs.
        bytes32 vs = bytes32(uint256(s) | (uint256(v - 27) << 255));
        bytes memory sig = abi.encodePacked(r, vs);
        assertEq(sig.length, 64);
        assertEq(Ecdsa.recover(h, sig), signer);
    }

    function test_Recover_WrongDigest_DoesNotMatch() public {
        bytes32 h1 = keccak256("one");
        bytes32 h2 = keccak256("two");
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(PK, h1);
        bytes memory sig = abi.encodePacked(r, s, v);
        address got = Ecdsa.recover(h2, sig);
        assertTrue(got != signer, "digest binding failed");
    }

    function test_Recover_MalleableHighS_Rejected() public {
        bytes32 h = keccak256("malleable");
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(PK, h);

        // Build the EIP-2 malleable twin: s' = n - s, flipped parity.
        uint256 sPrime = N - uint256(s);
        uint8 vPrime = v == 27 ? 28 : 27;
        bytes memory malleable = abi.encodePacked(r, bytes32(sPrime), vPrime);

        // The twin still recovers to the same key via raw ecrecover...
        address raw = ecrecover(h, vPrime, r, bytes32(sPrime));
        assertEq(raw, signer, "twin should recover to signer with raw ecrecover");

        // ...but our library rejects it because s is in the upper half.
        (address recovered, bool ok) = Ecdsa.tryRecover(h, malleable);
        assertFalse(ok);
        assertEq(recovered, address(0));
    }

    function test_Recover_BadLength_ReturnsFalse() public {
        bytes32 h = keccak256("len");
        bytes memory tooShort = new bytes(63);
        bytes memory tooLong = new bytes(66);
        (, bool ok1) = Ecdsa.tryRecover(h, tooShort);
        (, bool ok2) = Ecdsa.tryRecover(h, tooLong);
        assertFalse(ok1);
        assertFalse(ok2);
    }

    function test_Recover_BadV_ReturnsFalse() public {
        bytes32 h = keccak256("bad-v");
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(PK, h);
        bytes memory sig = abi.encodePacked(r, s, uint8(29)); // illegal v
        (, bool ok) = Ecdsa.tryRecover(h, sig);
        assertFalse(ok);
    }

    function test_Recover_ZeroS_ReturnsFalse() public {
        bytes32 h = keccak256("zero-s");
        (uint8 v, bytes32 r,) = vm.sign(PK, h);
        bytes memory sig = abi.encodePacked(r, bytes32(0), v);
        (, bool ok) = Ecdsa.tryRecover(h, sig);
        assertFalse(ok);
    }

    function test_Recover_VZeroOneNormalized() public {
        bytes32 h = keccak256("normalized-v");
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(PK, h);
        // 0/1 form must be normalized to 27/28 and accepted.
        bytes memory sig = abi.encodePacked(r, s, uint8(v - 27));
        assertEq(Ecdsa.recover(h, sig), signer);
    }
}
