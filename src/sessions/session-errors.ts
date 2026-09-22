import { HttpException, HttpStatus } from '@nestjs/common';

// 409-style domain errors used for idempotency conflicts, optimistic-version
// mismatches and illegal state transitions.
export class SessionConflictException extends HttpException {
  constructor(code: string, message: string, status: HttpStatus = HttpStatus.CONFLICT) {
    super({ error: code, message }, status);
  }
}

export const Errors = {
  notFound: (what: string) => new SessionConflictException(what.toUpperCase() + '_NOT_FOUND', `${what} not found`, HttpStatus.NOT_FOUND),
  ended: () =>
    new SessionConflictException('SESSION_ENDED', 'The session has ended and cannot be resumed'),
  forbidden: (message = 'Permission denied') =>
    new SessionConflictException('FORBIDDEN', message, HttpStatus.FORBIDDEN),
  badRequest: (message: string) =>
    new SessionConflictException('BAD_REQUEST', message, HttpStatus.BAD_REQUEST),
  versionConflict: (expected: number, actual: number) =>
    new SessionConflictException(
      'VERSION_CONFLICT',
      `Expected session version ${expected} but server is at ${actual}`,
    ),
  idempotencyConflict: () =>
    new SessionConflictException(
      'IDEMPOTENCY_CONFLICT',
      'A command with this requestId was already processed with a different body',
    ),
  sessionFull: () =>
    new SessionConflictException('SESSION_FULL', 'The session already has the maximum number of participants'),
  invalidTransition: (from: string, command: string) =>
    new SessionConflictException(
      'INVALID_TRANSITION',
      `Command ${command} is not allowed in state ${from}`,
    ),
};
