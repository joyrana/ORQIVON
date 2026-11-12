// Command smoke creates one task and exits non-zero if that fails (used after deployment).
package main

import (
	"fmt"
	"os"

	"example.com/taskservice/tasks"
)

func main() {
	t, err := tasks.NewService().CreateTask("smoke", "")
	if err != nil || t.ID != 1 {
		fmt.Fprintln(os.Stderr, "smoke failed:", err)
		os.Exit(1)
	}
	fmt.Println("ok")
}
