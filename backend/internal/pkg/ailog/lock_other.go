//go:build !unix

package ailog

import (
	"errors"
	"os"
)

func lockFile(_ *os.File) error { return errors.New("AI log WAL requires a Unix host") }
