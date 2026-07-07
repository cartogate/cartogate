package models

type Base struct {
	Name string
}

type User struct {
	Base
	age int
}

type Greeter interface {
	Greet() string
}

func NewUser(name string) *User {
	return &User{Base: Base{Name: name}}
}

func (u *User) Greet() string {
	return u.Name
}
