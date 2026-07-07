package auth

import (
	"fmt"

	"example.com/sample/models"
)

func Authenticate(name string) bool {
	return validate(name)
}

func validate(name string) bool {
	return len(name) > 0
}

func MakeUser(name string) *models.User {
	fmt.Println(name)
	return models.NewUser(name)
}
