package tasks

// ValidationError is returned when input to the task service is invalid.
type ValidationError struct {
	Field   string
	Message string
}

func (e *ValidationError) Error() string { return e.Field + ": " + e.Message }
