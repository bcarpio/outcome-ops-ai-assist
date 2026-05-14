package collisions

type A struct{}
type B struct{}

func (a *A) Helper() string {
	return "A"
}

func (b *B) Helper() string {
	return "B"
}

func helper() string {
	return "module"
}

func Caller() {
	helper()
}
