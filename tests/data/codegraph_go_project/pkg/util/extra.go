package util

// MakeTwo exercises the same-package cross-file call path.
func MakeTwo() int {
	g := NewGreeter("two")
	_ = g.Greet("x")
	return 2
}
