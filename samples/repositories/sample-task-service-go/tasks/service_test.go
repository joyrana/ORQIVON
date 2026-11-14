package tasks

import "testing"

func TestCreateAndList(t *testing.T) {
	s := NewService()
	task, err := s.CreateTask("Buy milk", "2 litres")
	if err != nil || task.ID != 1 || task.Title != "Buy milk" {
		t.Fatalf("unexpected %+v %v", task, err)
	}
	if got := s.ListTasks(); len(got) != 1 || got[0] != task {
		t.Fatalf("list = %v", got)
	}
}

func TestCompleteUnknownTask(t *testing.T) {
	if _, err := NewService().CompleteTask(42); err == nil {
		t.Fatal("expected an error")
	}
}
