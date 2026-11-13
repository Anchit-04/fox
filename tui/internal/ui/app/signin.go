package app

import (
	"os"
	"os/exec"
	"path/filepath"

	tea "charm.land/bubbletea/v2"

	"fox/tui/internal/protocol"
)

var loginCommands = map[string][]string{
	"claude-code": {"claude", "auth", "login"},
	"codex":       {"codex", "login"},
}

type signedInMsg struct {
	sessionID string
	taskID    string
	model     string
	err       error
}

func findCLI(name string) (string, error) {
	if path, err := exec.LookPath(name); err == nil {
		return path, nil
	}
	if appdata := os.Getenv("APPDATA"); appdata != "" {
		for _, suffix := range []string{".cmd", ".exe"} {
			candidate := filepath.Join(appdata, "npm", name+suffix)
			if _, err := os.Stat(candidate); err == nil {
				return candidate, nil
			}
		}
	}
	return "", exec.ErrNotFound
}

func (m *Model) signIn() tea.Cmd {
	task := m.store.NeedsSignIn(m.active, m.activeTask)
	if task == nil {
		m.flash = "nothing in this chat needs a sign-in"
		return nil
	}
	args, ok := loginCommands[task.SignIn]
	if !ok {
		m.flash = task.SignIn + " has no sign-in command Fox knows about"
		return nil
	}
	cli, err := findCLI(args[0])
	if err != nil {
		m.flash = args[0] + " isn't installed"
		return nil
	}
	sessionID, taskID, model := m.active, task.ID, task.SignIn
	return tea.ExecProcess(exec.Command(cli, args[1:]...), func(err error) tea.Msg {
		return signedInMsg{sessionID: sessionID, taskID: taskID, model: model, err: err}
	})
}

func (m *Model) onSignedIn(msg signedInMsg) tea.Cmd {
	if msg.err != nil {
		m.flash = "sign-in didn't finish: " + msg.err.Error() + " · ctrl+l tries again"
		return nil
	}
	m.store.SignedIn(msg.model)
	text := m.store.LastMessageTo(msg.sessionID, msg.taskID)
	if text == "" {
		m.flash = "signed in to " + msg.model + " · send a message to retry"
		return nil
	}
	m.flash = "signed in to " + msg.model + " · sending your last message again"
	id := msg.taskID
	m.store.AddUserMessage(msg.sessionID, id, text)
	return m.send.SendCmd(protocol.HumanMessage{SessionID: msg.sessionID, TaskID: &id, Content: text})
}
