import { randomBytes, randomInt } from 'crypto';

/** Unambiguous alphabet (no 0/O, 1/I/L) for human-typed join codes. */
const JOIN_CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';

export function generateJoinCode(length = 6): string {
  let code = '';
  for (let i = 0; i < length; i++) {
    code += JOIN_CODE_ALPHABET[randomInt(JOIN_CODE_ALPHABET.length)];
  }
  return code;
}

export function generateToken(): string {
  return randomBytes(24).toString('hex');
}
