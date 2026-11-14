// Command version prints the service version (used as a health check).
package main

import (
	"fmt"

	"example.com/taskservice/tasks"
)

func main() { fmt.Println(tasks.Version) }
