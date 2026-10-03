package app

import "testing"

func TestRun(t *testing.T) {
	if Run() == "" {
		t.Fail()
	}
}
