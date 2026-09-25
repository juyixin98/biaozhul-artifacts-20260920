package main

import (
	"fmt"
	"os"

	tailsampling "tailsampling"
)

func main() {
	if err := tailsampling.Run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "tailsampling:", err)
		os.Exit(1)
	}
}
