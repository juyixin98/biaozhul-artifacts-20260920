fn main() {
    // Compiles methods/guest for riscv32im-risc0-zkvm-elf with the rzup
    // toolchain, computes the image ID, and generates:
    //   BATCH_MEDIAN_ELF  — embedded guest ELF bytes
    //   BATCH_MEDIAN_ID   — image ID as [u32; 8]
    risc0_build::embed_methods();
}
