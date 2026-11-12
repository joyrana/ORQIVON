package tasks

// ValidationError is returned when input to the task service is invalid.
type ValidationError struct {
	Message string
}

func (e *ValidationError) Error() string { return e.Message }
