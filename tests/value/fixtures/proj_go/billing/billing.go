package billing

// Validate here is a DIFFERENT symbol than auth.Validate (a name-grep false positive).
func Validate(amount int) bool {
	return amount > 0
}

// Charge authorizes an amount.
func Charge(amount int, currency string) bool {
	return Validate(amount)
}
