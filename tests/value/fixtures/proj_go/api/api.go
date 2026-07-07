package api

import "example.com/proj_go/auth"

// Check validates a name through the auth package.
func Check(name string) bool {
	return auth.Validate(name)
}
