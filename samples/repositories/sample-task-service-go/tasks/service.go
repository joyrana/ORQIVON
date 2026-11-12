// Package tasks is an in-memory task store.
package tasks

import "fmt"

// Task is a unit of work.
type Task struct {
	ID          int
	Title       string
	Description string
	Done        bool
}

// Service stores tasks in memory.
type Service struct {
	tasks  []*Task
	nextID int
}

// NewService returns an empty store.
func NewService() *Service { return &Service{nextID: 1} }

// CreateTask stores a new task.
func (s *Service) CreateTask(title, description string) (*Task, error) {
	t := &Task{ID: s.nextID, Title: title, Description: description}
	s.tasks = append(s.tasks, t)
	s.nextID++
	return t, nil
}

// ListTasks returns every stored task.
func (s *Service) ListTasks() []*Task { return append([]*Task(nil), s.tasks...) }

// CompleteTask marks a task as done.
func (s *Service) CompleteTask(id int) (*Task, error) {
	for _, t := range s.tasks {
		if t.ID == id {
			t.Done = true
			return t, nil
		}
	}
	return nil, &ValidationError{Message: fmt.Sprintf("unknown task %d", id)}
}
