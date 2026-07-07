package auth

import "testing"

// TestValidate exercises Validate.
func TestValidate(t *testing.T) {
	if !Validate("alice") {
		t.Fatal("expected valid")
	}
}

// TestAuthenticate exercises Authenticate.
func TestAuthenticate(t *testing.T) {
	if !Authenticate("bob") {
		t.Fatal("expected authenticated")
	}
}
