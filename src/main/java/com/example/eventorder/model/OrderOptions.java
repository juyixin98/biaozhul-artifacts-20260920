package com.example.eventorder.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

/**
 * Optional knobs of a reconstruction request. Null components fall back to defaults
 * (see {@link com.example.eventorder.engine.OrderEngine}).
 *
 * @param enforceVersionOrder  when true, every dependency before-&gt;after whose endpoints both
 *                             carry a version must satisfy version(before) &lt;= version(after)
 * @param maxEnumeratedOrders  cap on the number of concrete valid orders listed in the response
 */
@JsonIgnoreProperties(ignoreUnknown = false)
public record OrderOptions(Boolean enforceVersionOrder, Integer maxEnumeratedOrders) {
}
