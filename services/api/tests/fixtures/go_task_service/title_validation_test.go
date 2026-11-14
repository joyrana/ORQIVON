package tasks

import "testing"

func TestRejectsEmptyTitle(t *testing.T) {
	if _, err := NewService().CreateTask("   ", ""); err == nil {
		t.Fatal("expected an error")
	}
}

func TestTrimsTitle(t *testing.T) {
	task, err := NewService().CreateTask("  Buy milk  ", "")
	if err != nil || task.Title != "Buy milk" {
		t.Fatalf("got %+v, %v", task, err)
	}
}
