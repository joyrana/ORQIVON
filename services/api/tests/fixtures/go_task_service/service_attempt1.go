// Package tasks is an in-memory task store.
package tasks

import (
	"fmt"
	"strings"
	"unicode/utf8"
)

// MaxTitleLength is the longest title accepted, in characters.
const MaxTitleLength = 120

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

// CreateTask validates the title, then stores a new task.
func (s *Service) CreateTask(title, description string) (*Task, error) {
	normalized := strings.TrimSpace(title)
	if normalized == "" {
		return nil, &ValidationError{Field: "title", Message: "title must not be empty"}
	}
	if utf8.RuneCountInString(normalized) >= MaxTitleLength {
		return nil, &ValidationError{Field: "title", Message: fmt.Sprintf("title must be at most %d characters", MaxTitleLength)}
	}
	t := &Task{ID: s.nextID, Title: normalized, Description: description}
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
	return nil, &ValidationError{Field: "id", Message: fmt.Sprintf("unknown task %d", id)}
}
