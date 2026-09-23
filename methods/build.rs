fn main() {
    // Compiles every guest listed in `[package.metadata.risc0].methods` for the
    // rv32im target and generates MEDIAN_GUEST_ELF/ID and DECOY_GUEST_ELF/ID.
    risc0_build::embed_methods();
}
