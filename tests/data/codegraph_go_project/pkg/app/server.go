package app

import (
	"fmt"

	"example.com/mesh/pkg/util"
)

type Server struct {
	Name string
}

func (s *Server) Handle(name string) string {
	g := util.NewGreeter(s.Name)
	return g.Greet(name)
}

func Run() string {
	s := &Server{Name: "main"}
	fmt.Println(s.Handle("world"))
	return "done"
}
