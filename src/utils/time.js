'use strict';

// 组织时区下的“日界”计算：返回 UTC Date，表示该时区本地日 00:00。
// 每人每天次数限制按“组织时区的自然日”计数，而非 UTC 日。
//
// 算法：先以 UTC 对齐到日，再按时区偏移校正；Intl 给出的偏移会自动覆盖夏令时，
// 迭代一次即可消除 DST 间隙/重叠造成的偏差。
function startOfDayInZone(date, timeZone) {
  const d = date instanceof Date ? date : new Date(date);
  // local = utc + offset；对 local 向下对齐到日，再减回偏移得到该时区 00:00 的 UTC 时刻。
  // 迭代一次以消除 DST 间隙/重叠造成的偏差。
  let shift = getOffsetMs(d, timeZone);
  let guess = new Date(Math.floor((d.getTime() + shift) / 86400000) * 86400000 - shift);
  shift = getOffsetMs(guess, timeZone);
  guess = new Date(Math.floor((d.getTime() + shift) / 86400000) * 86400000 - shift);
  return guess;
}

// 某时刻在目标时区相对 UTC 的偏移（毫秒）。东区（如 Asia/Shanghai）为正。
function getOffsetMs(date, timeZone) {
  const dtf = new Intl.DateTimeFormat('en-US', {
    timeZone,
    hourCycle: 'h23',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
  const parts = Object.fromEntries(dtf.formatToParts(date).filter((p) => p.type !== 'literal').map((p) => [p.type, p.value]));
  const asUtc = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour === 24 ? 0 : +parts.hour, +parts.minute, +parts.second);
  return asUtc - date.getTime();
}

// 组织时区自然日的 [start, nextStart)（均为 UTC Date）
function dayBoundsInZone(date, timeZone) {
  const start = startOfDayInZone(date, timeZone);
  return { start, end: new Date(start.getTime() + 86400000) };
}

// 在 UTC 日历上加 n 个整月（用于证书 12 个月有效期，规则明确、与时区/DST 无关）
function addUtcMonths(date, months) {
  const d = new Date(date.getTime());
  const day = d.getUTCDate();
  d.setUTCMonth(d.getUTCMonth() + months);
  // 如目标月天数不足（如 1/31 + 1 月），回落到该月最后一天
  if (d.getUTCDate() < day) d.setUTCDate(0);
  return d;
}

module.exports = { startOfDayInZone, dayBoundsInZone, addUtcMonths, getOffsetMs };
