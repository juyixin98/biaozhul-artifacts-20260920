package com.example.itasset.domain;

/**
 * Depreciation methods.
 * <ul>
 *   <li>SL  - straight line: constant monthly charge over the useful life.</li>
 *   <li>DDB - double declining balance: monthly charge = opening NBV * fixed monthly rate.</li>
 * </ul>
 */
public enum DepreciationMethod {
    SL,
    DDB
}
