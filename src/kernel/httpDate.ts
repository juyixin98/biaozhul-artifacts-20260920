/**
 * IMF-fixdate（RFC 9110 5.6.7）解析与格式化。
 * If-Range / Last-Modified 比较必须在同一日期表示上进行，
 * 因此这里不依赖 Date 的字符串比较，而是显式格式化后逐字符比较。
 */

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'] as const;
const MONTHS = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
] as const;

const IMF_FIXDATE_RE =
  /^(?:Sun|Mon|Tue|Wed|Thu|Fri|Sat), (\d{2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4}) (\d{2}):(\d{2}):(\d{2}) GMT$/;

/** 格式化为 IMF-fixdate，例如 Sun, 27 Sep 2026 12:34:56 GMT。 */
export function formatImfFixdate(epochMs: number): string {
  const d = new Date(epochMs);
  if (Number.isNaN(d.getTime())) {
    throw new Error(`无法格式化非法时间戳: ${epochMs}`);
  }
  const day = DAYS[d.getUTCDay()];
  const month = MONTHS[d.getUTCMonth()];
  const dd = String(d.getUTCDate()).padStart(2, '0');
  const yyyy = String(d.getUTCFullYear()).padStart(4, '0');
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  const ss = String(d.getUTCSeconds()).padStart(2, '0');
  return `${day}, ${dd} ${month} ${yyyy} ${hh}:${mm}:${ss} GMT`;
}

/**
 * 解析 IMF-fixdate；容忍被废弃的 RFC 850 / asctime 形式（RFC 9110 要求接收方宽容）。
 * 失败返回 null（调用方按“无法判定 → 回完整表示”处理）。
 */
export function parseHttpDate(raw: string): number | null {
  const value = raw.trim();
  const imf = IMF_FIXDATE_RE.exec(value);
  if (imf) {
    const [, dd, mon, yyyy, hh, mm, ss] = imf;
    if (dd === undefined || mon === undefined || yyyy === undefined ||
        hh === undefined || mm === undefined || ss === undefined) {
      return null;
    }
    const monthIndex = MONTHS.indexOf(mon as (typeof MONTHS)[number]);
    const epoch = Date.UTC(
      Number(yyyy),
      monthIndex,
      Number(dd),
      Number(hh),
      Number(mm),
      Number(ss),
    );
    return monthIndex < 0 || Number.isNaN(epoch) ? null : epoch;
  }
  // 回退：Date.parse 仅用于两种废弃历史格式；GMT/UTC 之外的时区一律拒绝。
  if (/GMT|UTC/i.test(value)) {
    const epoch = Date.parse(value.replace(/UTC/i, 'GMT'));
    return Number.isNaN(epoch) ? null : epoch;
  }
  return null;
}

/** 秒级等价判断（HTTP 日期分辨率为 1 秒）。 */
export function sameHttpDateInstant(leftEpochMs: number, rightEpochMs: number): boolean {
  return Math.floor(leftEpochMs / 1000) === Math.floor(rightEpochMs / 1000);
}
