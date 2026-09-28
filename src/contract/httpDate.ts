/**
 * Contract layer — HTTP-date handling for If-Modified-Since /
 * If-Unmodified-Since (RFC 9110 5.6.7, 13.1.1, 13.1.4).
 */

export class HttpDateSyntaxError extends Error {
  override readonly name = "HttpDateSyntaxError";
}

const DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS: Record<string, number> = {
  Jan: 0, Feb: 1, Mar: 2, Apr: 3, May: 4, Jun: 5,
  Jul: 6, Aug: 7, Sep: 8, Oct: 9, Nov: 10, Dec: 11,
};

/**
 * Parse an IMF-fixdate (the only date format senders MUST generate) plus the
 * two tolerated obsolete forms (RFC 850, asctime). Returns epoch millis.
 * Throws HttpDateSyntaxError on any malformed/ambiguous input.
 */
export function parseHttpDate(raw: string): number {
  const value = raw.trim();
  const ms = parseImfFixdate(value) ?? parseObsoleteRfc850(value) ?? parseAsctime(value);
  if (ms === null) {
    throw new HttpDateSyntaxError(`Malformed HTTP-date: ${JSON.stringify(raw)}`);
  }
  return ms;
}

/** Sun, 06 Nov 1994 08:49:37 GMT */
function parseImfFixdate(value: string): number | null {
  const match =
    /^(?:Sun|Mon|Tue|Wed|Thu|Fri|Sat), (\d{2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4}) (\d{2}):(\d{2}):(\d{2}) GMT$/.exec(
      value,
    );
  if (!match) return null;
  const [, day, mon, year, hh, mm, ss] = match;
  return assemble(Date.UTC(+year!, MONTHS[mon!]!, +day!, +hh!, +mm!, +ss!), value);
}

/** Sunday, 06-Nov-94 08:49:37 GMT */
function parseObsoleteRfc850(value: string): number | null {
  const match =
    /^(?:Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday), (\d{2})-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-(\d{2}) (\d{2}):(\d{2}):(\d{2}) GMT$/.exec(
      value,
    );
  if (!match) return null;
  const [, day, mon, yy, hh, mm, ss] = match;
  // RFC 9110: two-digit years in the obsolete format belong to 1900..1999.
  const year = 1900 + Number(yy);
  return assemble(Date.UTC(year, MONTHS[mon!]!, +day!, +hh!, +mm!, +ss!), value);
}

/** Sun Nov  6 08:49:37 1994 */
function parseAsctime(value: string): number | null {
  const match =
    /^(?:Sun|Mon|Tue|Wed|Thu|Fri|Sat) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) ([ \d]\d) (\d{2}):(\d{2}):(\d{2}) (\d{4})$/.exec(
      value,
    );
  if (!match) return null;
  const [, mon, day, hh, mm, ss, year] = match;
  return assemble(Date.UTC(+year!, MONTHS[mon!]!, +day!, +hh!, +mm!, +ss!), value);
}

/** Reject impossible calendar dates and non-matching weekdays (defensive). */
function assemble(utcMs: number, original: string): number | null {
  const d = new Date(utcMs);
  if (Number.isNaN(d.getTime())) return null;
  // Verify the weekday label agrees with the actual date when present.
  const weekdayPrefix = original.slice(0, 3);
  if (DAY_NAMES.includes(weekdayPrefix) && DAY_NAMES[d.getUTCDay()] !== weekdayPrefix) {
    return null;
  }
  return utcMs;
}

/** Serialize an epoch-ms value as an IMF-fixdate. */
export function formatHttpDate(ms: number): string {
  const d = new Date(ms);
  const day = DAY_NAMES[d.getUTCDay()];
  const dayNum = String(d.getUTCDate()).padStart(2, "0");
  const monName = Object.keys(MONTHS).find((k) => MONTHS[k] === d.getUTCMonth())!;
  const year = d.getUTCFullYear();
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  const ss = String(d.getUTCSeconds()).padStart(2, "0");
  return `${day}, ${dayNum} ${monName} ${year} ${hh}:${mm}:${ss} GMT`;
}
