package mermaid

import (
	"errors"
	"strings"
)

var ErrUnsupported = errors.New("only flowcharts (graph / flowchart) and sequence diagrams are drawn in the terminal")

func Kind(src string) string {
	header, _ := split(src)
	fields := strings.Fields(header)
	if len(fields) == 0 {
		return ""
	}
	return fields[0]
}

func split(src string) (string, []string) {
	lines := strings.Split(strings.ReplaceAll(src, "\r\n", "\n"), "\n")
	for i, line := range lines {
		t := strings.TrimSpace(line)
		if t == "" || strings.HasPrefix(t, "%%") || t == "---" {
			continue
		}
		return t, lines[i+1:]
	}
	return "", nil
}

func Render(src string, maxWidth int) (string, error) {
	header, body := split(src)
	switch Kind(src) {
	case "graph", "flowchart":
		f, err := parseFlowchart(header, body)
		if err != nil {
			return "", err
		}
		first := f.render(f.dir)
		if maxWidth <= 0 || widest(first) <= maxWidth {
			return first, nil
		}
		other := "LR"
		if f.dir == "LR" {
			other = "TD"
		}
		second := f.render(other)
		if widest(second) < widest(first) {
			return second, nil
		}
		return first, nil
	case "sequenceDiagram":
		s, err := parseSequence(body)
		if err != nil {
			return "", err
		}
		return s.render(), nil
	case "":
		return "", errors.New("the diagram is empty")
	}
	return "", ErrUnsupported
}

func widest(s string) int {
	w := 0
	for _, line := range strings.Split(s, "\n") {
		w = max(w, width(line))
	}
	return w
}
