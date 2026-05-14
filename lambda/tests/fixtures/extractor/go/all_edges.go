package booking

import (
	"fmt"
	"github.com/foo/bar"
)

type Service struct{}

func (s *Service) Confirm() {
	fmt.Println("hello")
	bar.DoThing()
	helper()
}

func helper() string {
	return "h"
}

func TopLevel() {
	(&Service{}).Confirm()
	helper()
}
