// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {CPMMPair} from "./CPMMPair.sol";

/// @title Deploys and indexes one CPMMPair per unordered token pair.
contract CPMMFactory {
    mapping(address => mapping(address => address)) public getPair;
    address[] public allPairs;

    event PairCreated(address indexed token0, address indexed token1, address pair, uint256 allPairsLength);

    error IdenticalTokens();
    error ZeroAddress();
    error PairExists();

    function createPair(address tokenA, address tokenB) external returns (address pair) {
        if (tokenA == tokenB) revert IdenticalTokens();
        (address token0, address token1) = tokenA < tokenB ? (tokenA, tokenB) : (tokenB, tokenA);
        if (token0 == address(0)) revert ZeroAddress();
        if (getPair[token0][token1] != address(0)) revert PairExists();

        CPMMPair deployed = new CPMMPair(token0, token1);
        pair = address(deployed);
        getPair[token0][token1] = pair;
        getPair[token1][token0] = pair; // populate both directions
        allPairs.push(pair);
        emit PairCreated(token0, token1, pair, allPairs.length);
    }

    function allPairsLength() external view returns (uint256) {
        return allPairs.length;
    }
}
