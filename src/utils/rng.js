'use strict';

const crypto = require('crypto');

// 确定性伪随机数：mulberry32，种子由字符串经 FNV-1a 派生为 32 位整数。
// 同种子 => 同序列 => 组卷结果可复现。
function hashSeed(str) {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i += 1) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

function createRng(seedStr) {
  let a = hashSeed(String(seedStr));
  return function next() {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Fisher–Yates 洗牌（使用注入的 rng，保证确定性）
function shuffled(arr, rng) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i -= 1) {
    const j = Math.floor(rng() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

// 随机字符串（非安全场景，如证书号后缀）
function randomSuffix(bytes = 6) {
  return crypto.randomBytes(bytes).toString('hex');
}

module.exports = { createRng, shuffled, hashSeed, randomSuffix };
