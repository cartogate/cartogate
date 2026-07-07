package auth

import "testing"

func TestAuthenticate(t *testing.T) {
	if !Authenticate("alice") {
		t.Fatal("expected ok")
	}
}
