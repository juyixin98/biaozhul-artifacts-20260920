/**
 * Minimal ambient declarations for the JS-only ZK libraries used here.
 * We only type the surfaces we touch; snarkjs/circomlibjs ship no types.
 */
declare module "circomlibjs" {
  export interface PoseidonInstance {
    (inputs: Uint8Array | bigint[] | string[]): Uint8Array;
    F: {
      toObject: (x: Uint8Array | bigint | string) => bigint;
      toString: (x: Uint8Array | bigint) => string;
      e: (x: bigint | string) => Uint8Array;
    };
  }
  export function buildPoseidon(): Promise<PoseidonInstance>;
}

declare module "snarkjs" {
  export namespace groth16 {
    function prove(
      zkeyPath: string,
      wtnsPath: string,
      logger?: unknown,
    ): Promise<{ proof: object; publicSignals: string[] }>;
    function verify(
      vkey: object,
      publicSignals: (string | bigint)[],
      proof: object,
      logger?: unknown,
    ): Promise<boolean>;
  }
  export namespace wtns {
    function calculate(
      input: Record<string, unknown>,
      wasmFileName: string,
      wtnsFileName: string,
      options?: { sanityCheck?: boolean },
    ): Promise<void>;
  }
}

declare module "circom_runtime" {
  export interface WitnessCalculator {
    circom_version: () => number;
    calculateWTNSBin(input: Record<string, unknown>, sanityCheck?: boolean): Promise<Uint8Array>;
  }
  export function WitnessCalculatorBuilder(
    wasm: Uint8Array,
    options?: { sanityCheck?: boolean; logGetSignal?: boolean; logSetSignal?: boolean },
  ): Promise<WitnessCalculator>;
}
