package com.itasset.service;

/** 请求语义错误（400），如参数非法、期间不连续。 */
public class BusinessRuleException extends RuntimeException {
    public BusinessRuleException(String message) {
        super(message);
    }
}
