// Package util provides greeting helpers.
package util

import "fmt"

// Speaker is the interface Greeter satisfies implicitly.
type Speaker interface {
	Greet(name string) string
}

// Base is embedded by Greeter.
type Base struct {
	Title string
}

func (b Base) Describe() string {
	return b.Title
}

type Greeter struct {
	Base
	Loud bool
}

func NewGreeter(title string) *Greeter {
	return &Greeter{Base: Base{Title: title}}
}

func (g *Greeter) Greet(name string) string {
	if g.Loud {
		return fmt.Sprintf("HI %s", name)
	}
	return g.Describe() + " " + name
}

func Shout(g *Greeter, name string) string {
	return g.Greet(name)
}
