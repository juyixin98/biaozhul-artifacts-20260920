//
// test_main.cpp — runner entry point for the built-in unit test framework.
//
#include "framework.hpp"

int main(int argc, char** argv) {
    return ::ru::test::runAll(argc, argv);
}
