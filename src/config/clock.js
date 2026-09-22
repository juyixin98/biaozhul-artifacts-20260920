'use strict';

// 可控时钟：生产取系统时间，测试可冻结/快进。
// 规则中所有“当前时间”必须经此模块获取，禁止散落 new Date()。
let frozenAt = null;

const clock = {
  now() {
    return frozenAt ? new Date(frozenAt.getTime()) : new Date();
  },
  // 冻结在某时刻（不传则冻结在当前系统时刻）
  freeze(at) {
    frozenAt = at ? new Date(at) : new Date();
    return new Date(frozenAt.getTime());
  },
  // 在已冻结时刻上快进毫秒数（未冻结则先冻结于当前）
  tick(ms) {
    if (!frozenAt) frozenAt = new Date();
    frozenAt = new Date(frozenAt.getTime() + ms);
    return new Date(frozenAt.getTime());
  },
  reset() {
    frozenAt = null;
  },
  isFrozen() {
    return frozenAt !== null;
  },
};

module.exports = clock;
