package xhs

import "testing"

func TestParseCount(t *testing.T) {
	for in, want := range map[string]int{"1.2万": 12000, "999+": 999, "10w": 100000, "3k": 3000, "": 0} {
		if got := ParseCount(in); got != want {
			t.Errorf("%q: got %d want %d", in, got, want)
		}
	}
}
