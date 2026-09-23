import {
  CompareReport,
  Finding,
  SideEvidence,
  StorageEntry,
  StorageLayout,
  TypeEntry,
} from './types.js';

export interface CompareOptions {
  /**
   * Decides whether a top-level variable is a reserved-storage "gap"
   * (OpenZeppelin `uint256[N] private __gap` convention).
   * Default: label is exactly `__gap`.
   */
  isGapVariable?: (entry: StorageEntry) => boolean;
}

const SLOT_BYTES = 32n;

function toBig(v: string | number): bigint {
  return BigInt(v);
}

function contractName(entry: { contract: string }): string {
  const i = entry.contract.lastIndexOf(':');
  return i >= 0 ? entry.contract.slice(i + 1) : entry.contract;
}

/** Number of 32-byte slots a top-level entry of this type occupies. */
function slotsOccupied(t: TypeEntry | undefined): bigint {
  if (!t) return 1n; // unknown: assume one slot, and UNKNOWN_TYPE is reported elsewhere
  if (t.encoding === 'inplace') {
    const bytes = BigInt(t.numberOfBytes ?? '32');
    return (bytes + SLOT_BYTES - 1n) / SLOT_BYTES;
  }
  // mapping / dynamic_array / bytes heads occupy exactly one slot
  return 1n;
}

/** Parse the length of a static array from its label, e.g. "uint256[50]". */
function staticArrayLength(label: string): bigint | undefined {
  const m = /\[(\d+)\]\s*$/.exec(label);
  return m ? BigInt(m[1]!) : undefined;
}

function sideOf(e: { slot: string; offset: number; type: string }, t?: TypeEntry): SideEvidence {
  return {
    slot: e.slot,
    offset: e.offset,
    type: e.type,
    typeLabel: t?.label,
    numberOfBytes: t?.numberOfBytes,
  };
}

class Checker {
  readonly findings: Finding[] = [];

  constructor(
    private readonly oldTypes: Record<string, TypeEntry>,
    private readonly newTypes: Record<string, TypeEntry>,
  ) {}

  private push(f: Finding): void {
    this.findings.push(f);
  }

  /**
   * Recursively compare two type ids. `path` is the shortest path to the
   * variable/member being compared; nested divergences extend it.
   */
  compareTypes(oldId: string, newId: string, path: string): void {
    if (oldId === newId) {
      // Identical type id => identical structure, unless the compiler gave us
      // no descriptor for it at all (then we cannot vouch for it).
      if (!this.oldTypes[oldId] && !this.newTypes[newId]) {
        this.push({
          severity: 'warning',
          code: 'UNKNOWN_TYPE',
          path,
          message: `type "${oldId}" is not described in either layout's "types" table; cannot verify`,
          evidence: { old: { type: oldId }, new: { type: newId } },
        });
      }
      return;
    }

    const ot = this.oldTypes[oldId];
    const nt = this.newTypes[newId];
    if (!ot || !nt) {
      this.push({
        severity: 'warning',
        code: 'UNKNOWN_TYPE',
        path,
        message: `type descriptor missing (${!ot ? `"${oldId}" in old layout` : `"${newId}" in new layout`}); cannot determine compatibility`,
        evidence: {
          old: { type: oldId, typeLabel: ot?.label },
          new: { type: newId, typeLabel: nt?.label },
        },
      });
      return;
    }

    if (ot.encoding !== nt.encoding) {
      this.push({
        severity: 'error',
        code: 'TYPE_KIND_CHANGED',
        path,
        message: `storage encoding changed from "${ot.encoding}" (${ot.label}) to "${nt.encoding}" (${nt.label})`,
        evidence: {
          old: { type: oldId, typeLabel: ot.label },
          new: { type: newId, typeLabel: nt.label },
        },
      });
      return;
    }

    switch (ot.encoding) {
      case 'inplace':
        if (ot.members && nt.members) {
          this.compareStructMembers(ot, nt, path);
        } else if (ot.base !== undefined && nt.base !== undefined) {
          this.compareStaticArray(ot, nt, path);
        } else if (ot.members || nt.members || ot.base !== undefined || nt.base !== undefined) {
          this.push({
            severity: 'error',
            code: 'TYPE_KIND_CHANGED',
            path,
            message: `type shape changed from "${ot.label}" to "${nt.label}"`,
            evidence: {
              old: { type: oldId, typeLabel: ot.label },
              new: { type: newId, typeLabel: nt.label },
            },
          });
        } else {
          this.compareValueType(ot, nt, oldId, newId, path);
        }
        return;
      case 'mapping':
        this.compareMapping(ot, nt, path);
        return;
      case 'dynamic_array':
        this.compareWrapped('ARRAY_BASE_CHANGED', ot.base, nt.base, `${path}[*]`, ot, nt, oldId, newId, path);
        return;
      case 'bytes':
        if (ot.label !== nt.label) {
          this.push({
            severity: 'error',
            code: 'TYPE_LABEL_CHANGED',
            path,
            message: `type changed from "${ot.label}" to "${nt.label}"`,
            evidence: {
              old: { type: oldId, typeLabel: ot.label },
              new: { type: newId, typeLabel: nt.label },
            },
          });
        }
        return;
      default:
        this.push({
          severity: 'warning',
          code: 'UNKNOWN_ENCODING',
          path,
          message: `unrecognized storage encoding "${ot.encoding}" for type "${ot.label}"; cannot determine compatibility`,
          evidence: {
            old: { type: oldId, typeLabel: ot.label },
            new: { type: newId, typeLabel: nt.label },
          },
        });
    }
  }

  private compareValueType(ot: TypeEntry, nt: TypeEntry, oldId: string, newId: string, path: string): void {
    if (ot.numberOfBytes !== nt.numberOfBytes) {
      this.push({
        severity: 'error',
        code: 'TYPE_WIDTH_CHANGED',
        path,
        message: `value type width changed from ${ot.numberOfBytes} bytes (${ot.label}) to ${nt.numberOfBytes} bytes (${nt.label})`,
        evidence: {
          old: { type: oldId, typeLabel: ot.label, numberOfBytes: ot.numberOfBytes },
          new: { type: newId, typeLabel: nt.label, numberOfBytes: nt.numberOfBytes },
        },
      });
      return;
    }
    if (ot.label !== nt.label) {
      this.push({
        severity: 'error',
        code: 'TYPE_LABEL_CHANGED',
        path,
        message: `value type changed from "${ot.label}" to "${nt.label}"`,
        evidence: {
          old: { type: oldId, typeLabel: ot.label },
          new: { type: newId, typeLabel: nt.label },
        },
      });
    }
  }

  private compareStructMembers(ot: TypeEntry, nt: TypeEntry, path: string): void {
    const oldMembers = ot.members ?? [];
    const newMembers = nt.members ?? [];
    const newByLabel = new Map(newMembers.map((m) => [m.label, m]));

    for (const om of oldMembers) {
      const nm = newByLabel.get(om.label);
      const mPath = `${path}.${om.label}`;
      if (!nm) {
        this.push({
          severity: 'error',
          code: 'STRUCT_MEMBER_REMOVED',
          path: mPath,
          message: `struct member "${om.label}" (${ot.label}) was removed`,
          evidence: { old: sideOf(om, this.oldTypes[om.type]) },
        });
        continue;
      }
      if (om.slot !== nm.slot || om.offset !== nm.offset) {
        this.push({
          severity: 'error',
          code: 'STRUCT_MEMBER_MOVED',
          path: mPath,
          message: `struct member "${om.label}" moved from slot ${om.slot} offset ${om.offset} to slot ${nm.slot} offset ${nm.offset}`,
          evidence: {
            old: sideOf(om, this.oldTypes[om.type]),
            new: sideOf(nm, this.newTypes[nm.type]),
          },
        });
      }
      this.compareTypes(om.type, nm.type, mPath);
    }

    // Members present only in the new struct must be appended after every old
    // member; anything else shifts existing members and is an insertion.
    let oldEnd = 0n;
    for (const om of oldMembers) {
      const end = toBig(om.slot) + slotsOccupied(this.oldTypes[om.type]);
      if (end > oldEnd) oldEnd = end;
    }
    const oldLabels = new Set(oldMembers.map((m) => m.label));
    for (const nm of newMembers) {
      if (oldLabels.has(nm.label)) continue;
      const mPath = `${path}.${nm.label}`;
      if (toBig(nm.slot) >= oldEnd) {
        this.push({
          severity: 'info',
          code: 'STRUCT_MEMBER_APPENDED',
          path: mPath,
          message: `struct member "${nm.label}" appended at slot ${nm.slot} offset ${nm.offset} (after all existing members)`,
          evidence: { new: sideOf(nm, this.newTypes[nm.type]) },
        });
      } else {
        this.push({
          severity: 'error',
          code: 'STRUCT_MEMBER_INSERTED',
          path: mPath,
          message: `struct member "${nm.label}" inserted at slot ${nm.slot} offset ${nm.offset}, before the end of the existing members (ends at slot ${oldEnd})`,
          evidence: { new: sideOf(nm, this.newTypes[nm.type]) },
        });
      }
    }
  }

  private compareStaticArray(ot: TypeEntry, nt: TypeEntry, path: string): void {
    const oldLen = staticArrayLength(ot.label);
    const newLen = staticArrayLength(nt.label);
    if (oldLen === undefined || newLen === undefined) {
      this.push({
        severity: 'warning',
        code: 'UNKNOWN_TYPE',
        path,
        message: `cannot parse static array length from "${ot.label}" / "${nt.label}"; cannot determine compatibility`,
        evidence: {
          old: { typeLabel: ot.label },
          new: { typeLabel: nt.label },
        },
      });
      return;
    }
    if (oldLen !== newLen) {
      this.push({
        severity: 'error',
        code: 'ARRAY_LENGTH_CHANGED',
        path,
        message: `static array length changed from ${oldLen} (${ot.label}) to ${newLen} (${nt.label})`,
        evidence: {
          old: { typeLabel: ot.label, numberOfBytes: ot.numberOfBytes },
          new: { typeLabel: nt.label, numberOfBytes: nt.numberOfBytes },
        },
      });
    }
    this.compareWrapped('ARRAY_BASE_CHANGED', ot.base, nt.base, `${path}[*]`, ot, nt, ot.label, nt.label, path);
  }

  private compareMapping(ot: TypeEntry, nt: TypeEntry, path: string): void {
    this.compareWrapped('MAPPING_TYPE_CHANGED', ot.key, nt.key, `${path}.<key>`, ot, nt, ot.label, nt.label, path);
    this.compareWrapped('MAPPING_TYPE_CHANGED', ot.value, nt.value, `${path}.<value>`, ot, nt, ot.label, nt.label, path);
  }

  /**
   * Compare a nested type reference (array base, mapping key/value). Emits a
   * summarizing error with `code` when the nested comparison fails, plus the
   * detailed nested findings.
   */
  private compareWrapped(
    code: 'ARRAY_BASE_CHANGED' | 'MAPPING_TYPE_CHANGED',
    oldId: string | undefined,
    newId: string | undefined,
    nestedPath: string,
    ot: TypeEntry,
    nt: TypeEntry,
    oldLabel: string,
    newLabel: string,
    path: string,
  ): void {
    if (oldId === undefined || newId === undefined) {
      this.push({
        severity: 'warning',
        code: 'UNKNOWN_TYPE',
        path: nestedPath,
        message: `missing base/key/value type reference on "${oldLabel}" / "${newLabel}"; cannot determine compatibility`,
        evidence: { old: { typeLabel: ot.label }, new: { typeLabel: nt.label } },
      });
      return;
    }
    if (oldId === newId) return;
    const before = this.findings.length;
    this.compareTypes(oldId, newId, nestedPath);
    const nested = this.findings.slice(before);
    if (nested.some((f) => f.severity === 'error')) {
      this.findings.splice(before, 0, {
        severity: 'error',
        code,
        path,
        message: `${code === 'ARRAY_BASE_CHANGED' ? 'array element' : 'mapping key/value'} type changed incompatibly: "${oldLabel}" -> "${newLabel}"`,
        evidence: { old: { typeLabel: oldLabel }, new: { typeLabel: newLabel } },
      });
    }
  }
}

interface GapInfo {
  entry: StorageEntry;
  startSlot: bigint;
  slots: bigint;
}

function detectGap(
  entry: StorageEntry,
  types: Record<string, TypeEntry>,
  isGap: (e: StorageEntry) => boolean,
): GapInfo | undefined {
  if (!isGap(entry)) return undefined;
  const t = types[entry.type];
  if (!t || t.encoding !== 'inplace' || t.base === undefined) return undefined;
  const slots = staticArrayLength(t.label);
  if (slots === undefined) return undefined;
  return { entry, startSlot: toBig(entry.slot), slots };
}

/** Compare two storage layouts and produce a compatibility report. */
export function compareLayouts(
  oldLayout: StorageLayout,
  newLayout: StorageLayout,
  options: CompareOptions = {},
): CompareReport {
  const isGap = options.isGapVariable ?? ((e: StorageEntry) => e.label === '__gap');
  const checker = new Checker(oldLayout.types ?? {}, newLayout.types ?? {});
  const findings = checker.findings;

  const oldStorage = oldLayout.storage ?? [];
  const newStorage = newLayout.storage ?? [];
  // The most-derived contract declares the last storage entries (solc lists
  // base-contract variables first), so it names the layout best.
  const root = oldStorage.length > 0 ? contractName(oldStorage[oldStorage.length - 1]!) : '<storage>';

  const oldGap = oldStorage.map((e) => detectGap(e, oldLayout.types ?? {}, isGap)).find((g) => g !== undefined);
  const newGap = newStorage.map((e) => detectGap(e, newLayout.types ?? {}, isGap)).find((g) => g !== undefined);

  const newByLabel = new Map(newStorage.map((e) => [e.label, e]));

  // 1. Every old variable must still exist at the same slot/offset.
  //    Gap variables are exempt from the moved-check: a consistent gap
  //    reduction legitimately shifts the gap array itself.
  for (const oe of oldStorage) {
    const path = `${root}.${oe.label}`;
    const ne = newByLabel.get(oe.label);
    if (!ne) {
      // A gap that vanished is handled by the gap bookkeeping below: it is
      // only legal if new variables fully consumed it.
      if (oldGap?.entry === oe) continue;
      findings.push({
        severity: 'error',
        code: 'FIELD_REMOVED',
        path,
        message: `state variable "${oe.label}" was removed`,
        evidence: { old: sideOf(oe, oldLayout.types?.[oe.type]) },
      });
      continue;
    }
    // Gap entries are checked by the gap bookkeeping below, not by the
    // generic slot/type comparison: a consistent gap reduction legitimately
    // moves the gap array and shrinks its length.
    const isGapEntry = oldGap?.entry === oe;
    if (isGapEntry) continue;
    if (oe.slot !== ne.slot || oe.offset !== ne.offset) {
      const crossedContract = oe.contract !== ne.contract;
      findings.push({
        severity: 'error',
        code: 'FIELD_MOVED',
        path,
        message:
          `state variable "${oe.label}" moved from slot ${oe.slot} offset ${oe.offset} ` +
          `to slot ${ne.slot} offset ${ne.offset}` +
          (crossedContract ? ` (declaring contract changed: ${oe.contract} -> ${ne.contract}; inheritance reorder)` : ''),
        evidence: {
          old: sideOf(oe, oldLayout.types?.[oe.type]),
          new: sideOf(ne, newLayout.types?.[ne.type]),
        },
      });
    }
    checker.compareTypes(oe.type, ne.type, path);
  }

  // 2. New variables must be appended after all old variables, or consume a
  //    declared storage gap contiguously from its start.
  const oldLabels = new Set(oldStorage.map((e) => e.label));
  const brandNew = newStorage.filter((e) => !oldLabels.has(e.label) && !(oldGap && e === newGap?.entry));

  // First slot that is genuinely free in the old layout (ignoring the gap,
  // which is handled separately).
  let oldEnd = 0n;
  for (const oe of oldStorage) {
    if (oldGap && oe === oldGap.entry) continue;
    const end = toBig(oe.slot) + slotsOccupied(oldLayout.types?.[oe.type]);
    if (end > oldEnd) oldEnd = end;
  }
  // If there is a gap, appended-after-gap slots are also free.
  const gapEnd = oldGap ? oldGap.startSlot + oldGap.slots : undefined;
  const freeAfter = gapEnd !== undefined && gapEnd > oldEnd ? gapEnd : oldEnd;

  let gapConsumed = 0n;
  for (const ne of brandNew) {
    const path = `${root}.${ne.label}`;
    const slot = toBig(ne.slot);
    const width = slotsOccupied(newLayout.types?.[ne.type]);
    const inGap = oldGap !== undefined && slot >= oldGap.startSlot && slot + width <= oldGap.startSlot + oldGap.slots;
    if (inGap) {
      const end = slot + width - oldGap!.startSlot;
      if (end > gapConsumed) gapConsumed = end;
      findings.push({
        severity: 'info',
        code: 'FIELD_APPENDED',
        path,
        message: `state variable "${ne.label}" added at slot ${ne.slot} offset ${ne.offset}, inside the reserved storage gap`,
        evidence: { new: sideOf(ne, newLayout.types?.[ne.type]) },
      });
    } else if (slot >= freeAfter) {
      findings.push({
        severity: 'info',
        code: 'FIELD_APPENDED',
        path,
        message: `state variable "${ne.label}" appended at slot ${ne.slot} offset ${ne.offset} (after all existing variables)`,
        evidence: { new: sideOf(ne, newLayout.types?.[ne.type]) },
      });
    } else {
      findings.push({
        severity: 'error',
        code: 'FIELD_INSERTED',
        path,
        message:
          `state variable "${ne.label}" inserted at slot ${ne.slot} offset ${ne.offset}, ` +
          `which is at or before existing storage (first free slot is ${freeAfter}); this shifts existing variables`,
        evidence: { new: sideOf(ne, newLayout.types?.[ne.type]) },
      });
    }
  }

  // 3. Gap bookkeeping: the new gap must start exactly where consumption ends
  //    and be shrunk by exactly the consumed amount.
  if (oldGap) {
    if (!newGap) {
      if (gapConsumed < oldGap.slots) {
        findings.push({
          severity: 'error',
          code: 'GAP_MISMATCH',
          path: `${root}.${oldGap.entry.label}`,
          message: `storage gap "${oldGap.entry.label}" (${oldGap.slots} slots at slot ${oldGap.startSlot}) disappeared but only ${gapConsumed} slot(s) were consumed by new variables`,
          evidence: { old: sideOf(oldGap.entry, oldLayout.types?.[oldGap.entry.type]) },
        });
      } else {
        findings.push({
          severity: 'info',
          code: 'GAP_REDUCED',
          path: `${root}.${oldGap.entry.label}`,
          message: `storage gap fully consumed by ${gapConsumed} slot(s) of new variables`,
          evidence: {
            old: sideOf(oldGap.entry, oldLayout.types?.[oldGap.entry.type]),
          },
        });
      }
    } else {
      const expectedStart = oldGap.startSlot + gapConsumed;
      const expectedSlots = oldGap.slots - gapConsumed;
      if (newGap.startSlot !== expectedStart || newGap.slots !== expectedSlots) {
        findings.push({
          severity: 'error',
          code: 'GAP_MISMATCH',
          path: `${root}.${newGap.entry.label}`,
          message:
            `storage gap moved/shrank inconsistently: expected slot ${expectedStart} with ${expectedSlots} slot(s) ` +
            `(old gap: slot ${oldGap.startSlot}, ${oldGap.slots} slots; consumed by new variables: ${gapConsumed}), ` +
            `found slot ${newGap.startSlot} with ${newGap.slots} slot(s)`,
          evidence: {
            old: sideOf(oldGap.entry, oldLayout.types?.[oldGap.entry.type]),
            new: sideOf(newGap.entry, newLayout.types?.[newGap.entry.type]),
          },
        });
      } else if (gapConsumed > 0n) {
        findings.push({
          severity: 'info',
          code: 'GAP_REDUCED',
          path: `${root}.${newGap.entry.label}`,
          message: `storage gap reduced from ${oldGap.slots} to ${newGap.slots} slot(s) to make room for ${gapConsumed} slot(s) of new variables`,
          evidence: {
            old: sideOf(oldGap.entry, oldLayout.types?.[oldGap.entry.type]),
            new: sideOf(newGap.entry, newLayout.types?.[newGap.entry.type]),
          },
        });
      }
    }
  }

  const errors = findings.filter((f) => f.severity === 'error').length;
  const warnings = findings.filter((f) => f.severity === 'warning').length;
  const infos = findings.filter((f) => f.severity === 'info').length;

  const verdict = errors > 0 ? 'incompatible' : warnings > 0 ? 'unknown' : 'compatible';
  const summary =
    verdict === 'compatible'
      ? `compatible: ${infos} append/gap change(s), no conflicts`
      : verdict === 'incompatible'
        ? `incompatible: ${errors} error(s), first at ${findings.find((f) => f.severity === 'error')?.path ?? '<unknown>'}`
        : `unknown: ${warnings} unverifiable type(s); cannot prove compatibility`;

  return { verdict, summary, findings, stats: { errors, warnings, infos } };
}
