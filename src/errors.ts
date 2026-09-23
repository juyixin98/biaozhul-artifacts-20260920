/** Typed errors carrying stable codes for the API layer. */
export class AppError extends Error {
  constructor(
    public code: string,
    message: string,
    public statusCode = 400,
    public details?: unknown,
  ) {
    super(message);
    this.name = 'AppError';
  }
}

export const err = {
  badRequest: (message: string, details?: unknown) => new AppError('E_BAD_REQUEST', message, 400, details),
  unsupportedCid: (message: string) => new AppError('E_UNSUPPORTED_CID', message, 400),
  unsupportedEncoding: (message: string) => new AppError('E_UNSUPPORTED_ENCODING', message, 400),
  blockTooLarge: (size: number, max: number) =>
    new AppError('E_BLOCK_TOO_LARGE', `block size ${size} exceeds limit ${max}`, 413, { size, max }),
  notFound: (message: string) => new AppError('E_NOT_FOUND', message, 404),
  conflict: (message: string, details?: unknown) => new AppError('E_CONFLICT', message, 409, details),
};
