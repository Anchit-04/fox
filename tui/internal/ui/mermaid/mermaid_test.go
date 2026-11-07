package mermaid

import (
	"errors"
	"strings"
	"testing"
)

func render(t *testing.T, src string, width int) string {
	t.Helper()
	out, err := Render(src, width)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	return out
}

func has(t *testing.T, out string, parts ...string) {
	t.Helper()
	for _, p := range parts {
		if !strings.Contains(out, p) {
			t.Fatalf("missing %q in:\n%s", p, out)
		}
	}
}

func TestALeftToRightChain(t *testing.T) {
	out := render(t, "graph LR\n  TUI --> Hub\n  Hub --> DB[(fox.db)]", 0)
	has(t, out, "│ TUI ├", "►│ Hub ├", "►│ fox.db │", "╭────────╮")
	if strings.Count(out, "\n") != 2 {
		t.Fatalf("expected one row of boxes:\n%s", out)
	}
}

func TestATopDownTreeWithLabels(t *testing.T) {
	out := render(t, "graph TD\n  A[Orchestrator] -->|delegate| B[Executor 1]\n  A -->|delegate| C[Executor 2]\n  B --> M[Shared memory]\n  C --> M", 0)
	has(t, out, "│ Orchestrator │", "┬", "▼ delegate", "│ Executor 1 │", "│ Executor 2 │", "│ Shared memory │")
	lines := strings.Split(out, "\n")
	if !strings.Contains(lines[len(lines)-2], "Shared memory") {
		t.Fatalf("the shared node should be last:\n%s", out)
	}
}

func TestALoopComesBackOnTheSide(t *testing.T) {
	out := render(t, "flowchart TD\n  Q{tests pass?} -- yes --> P[push]\n  Q -- no --> F[fix]\n  F --> Q", 0)
	has(t, out, "⟨ tests pass? ⟩◄", "│ fix ├", "yes", "no")
}

func TestChainsGroupsAndShapes(t *testing.T) {
	out := render(t, "graph LR; A(round) & B --> C{choice} -.-> D", 0)
	has(t, out, "╭───────╮", "⟨ choice ⟩", "┄")
}

func TestAWideLeftToRightFallsBackToTopDown(t *testing.T) {
	src := "graph LR\n  first_service --> second_service --> third_service --> fourth_service"
	wide := render(t, src, 0)
	narrow := render(t, src, 40)
	if widest(narrow) >= widest(wide) || strings.Count(narrow, "\n") <= strings.Count(wide, "\n") {
		t.Fatalf("did not switch to top-down:\n%s", narrow)
	}
}

func TestASequenceDiagram(t *testing.T) {
	out := render(t, "sequenceDiagram\n  autonumber\n  participant T as TUI\n  T->>Hub: file_diff\n  Hub-->>T: files\n  Note over Hub: thinks\n  Hub->>Hub: cache\n  loop every edit\n  T-xHub: stop\n  end", 0)
	has(t, out, "│ TUI │", "│ Hub │", "1. file_diff", "───►", "◄┄", "│ thinks │", "◄─┘", "▸ loop every edit", "×")
}

func TestUnsupportedAndBrokenDiagrams(t *testing.T) {
	if _, err := Render("classDiagram\n  A <|-- B", 80); !errors.Is(err, ErrUnsupported) {
		t.Fatalf("err = %v", err)
	}
	if _, err := Render("graph LR\n  A -->", 80); err == nil || !strings.Contains(err.Error(), "line 2") {
		t.Fatalf("err = %v", err)
	}
	if _, err := Render("graph LR\n  A[open", 80); err == nil || !strings.Contains(err.Error(), "closing") {
		t.Fatalf("err = %v", err)
	}
	if _, err := Render("  \n", 80); err == nil {
		t.Fatal("an empty diagram rendered")
	}
}

func TestCommentsAndStylingAreIgnored(t *testing.T) {
	out := render(t, "%% top\ngraph TD\n  classDef hot fill:#f00\n  A --> B\n  style A fill:#fff\n  %% note\n  subgraph S\n  B --> C\n  end", 0)
	has(t, out, "│ A │", "│ B │", "│ C │")
}
