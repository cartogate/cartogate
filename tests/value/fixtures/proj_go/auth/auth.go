package auth

// Validate reports whether name is non-empty.
func Validate(name string) bool {
	return len(name) > 0
}

// Authenticate checks a name by validating it.
func Authenticate(name string) bool {
	return Validate(name)
}
