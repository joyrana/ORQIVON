package tasks

import (
	"errors"
	"strings"
	"testing"
)

func TestFR_001_AC1_RejectsTitleLongerThan120(t *testing.T) {
	if _, err := NewService().CreateTask(strings.Repeat("a", 121), ""); err == nil {
		t.Fatal("a 121-character title was accepted")
	}
}

func TestFR_001_AC2_AcceptsTitleOfExactly120(t *testing.T) {
	task, err := NewService().CreateTask(strings.Repeat("a", 120), "")
	if err != nil {
		t.Fatalf("a 120-character title was rejected: %v", err)
	}
	if len(task.Title) != 120 {
		t.Fatalf("title length %d", len(task.Title))
	}
}

func TestFR_002_AC1_TrimsSurroundingWhitespace(t *testing.T) {
	task, err := NewService().CreateTask("  Buy milk  ", "")
	if err != nil || task.Title != "Buy milk" {
		t.Fatalf("got %+v, %v", task, err)
	}
}

func TestFR_003_AC1_RejectsOnlySpaces(t *testing.T) {
	if _, err := NewService().CreateTask("     ", ""); err == nil {
		t.Fatal("a title of only spaces was accepted")
	}
}

func TestNFR_002_AC1_ErrorNamesTheField(t *testing.T) {
	_, err := NewService().CreateTask("", "")
	var verr *ValidationError
	if !errors.As(err, &verr) || verr.Field != "title" || len(strings.Fields(verr.Message)) < 3 {
		t.Fatalf("unexpected error %#v", err)
	}
}
