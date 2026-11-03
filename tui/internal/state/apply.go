package state

import (
	"strings"

	"fox/tui/internal/protocol"
)

func (s *Store) Apply(ev protocol.Event) {
	switch ev.Type {
	case protocol.Models:
		if p, err := protocol.Payload[protocol.ModelsPayload](ev); err == nil {
			s.Models = p.Models
		}
		return
	case protocol.Attached:
		s.applyAttached(ev)
		return
	case protocol.SessionStatus, protocol.Projects, protocol.Sessions:
		return
	}

	if ev.SessionID == "" {
		if ev.Type == protocol.Error {
			if p, err := protocol.Payload[protocol.ErrorPayload](ev); err == nil {
				s.Notices = append(s.Notices, p.Message)
			}
		}
		return
	}

	sess := s.ensure(ev.SessionID)
	if ev.Seq > 0 {
		if ev.Seq <= sess.lastSeq {
			return
		}
		sess.lastSeq = ev.Seq
	}
	taskID := ""
	if ev.TaskID != nil {
		taskID = *ev.TaskID
	}

	switch ev.Type {
	case protocol.SessionStarted:
		s.applySessionStarted(sess, ev)
	case protocol.SessionEnded:
		s.applySessionEnded(sess, ev)
	case protocol.AgentTurn:
		s.applyAgentTurn(sess, taskID, ev)
	case protocol.HumanMessageInjected:
		applyInjected(sess, taskID, ev)
		if taskID == "" {
			s.setStatus(sess, Working)
		}
	case protocol.ToolCall:
		if p, err := protocol.Payload[protocol.ToolCallPayload](ev); err == nil {
			sess.add(Entry{Kind: ToolCallEntry, TaskID: taskID, Tool: p.Name, Args: p.Args})
			s.setStatus(sess, Working)
		}
	case protocol.ToolResult:
		if p, err := protocol.Payload[protocol.ToolResultPayload](ev); err == nil {
			sess.add(Entry{Kind: ToolResultEntry, TaskID: taskID, Tool: p.Name, Text: p.Content})
		}
		s.settleApprovals(sess.ID, taskID)
	case protocol.ToolDenied:
		if p, err := protocol.Payload[protocol.ToolDeniedPayload](ev); err == nil {
			sess.add(Entry{Kind: DeniedEntry, TaskID: taskID, Tool: p.Name, Text: p.Reason})
		}
		s.settleApprovals(sess.ID, taskID)
	case protocol.ApprovalRequested:
		if p, err := protocol.Payload[protocol.ApprovalRequestedPayload](ev); err == nil {
			s.Approvals = append(s.Approvals, Approval{SessionID: sess.ID, TaskID: taskID, ApprovalRequestedPayload: p})
			sess.Status = NeedsYou
		}
	case protocol.TodoUpdated:
		if p, err := protocol.Payload[protocol.TodoUpdatedPayload](ev); err == nil && taskID == "" {
			sess.Todos = p.Checklist
		}
	case protocol.TaskCreated:
		if p, err := protocol.Payload[protocol.TaskCreatedPayload](ev); err == nil && taskID != "" {
			t := sess.task(taskID)
			t.Description, t.Lane, t.Scope, t.Status = p.Description, p.Lane, p.Scope, "queued"
		}
	case protocol.TaskStatusChanged:
		applyTaskStatus(sess, taskID, ev)
	case protocol.ProposalMade:
		if p, err := protocol.Payload[protocol.ProposalMadePayload](ev); err == nil {
			sess.add(Entry{Kind: NoticeEntry, TaskID: taskID, Text: "proposal: " + p.Text})
		}
	case protocol.ScopeAudit:
		if p, err := protocol.Payload[protocol.ScopeAuditPayload](ev); err == nil && p.Mispredicted {
			sess.add(Entry{Kind: NoticeEntry, TaskID: taskID,
				Text: "wrote outside its declared scope: " + strings.Join(p.Undeclared, ", ")})
		}
	case protocol.ModelCalled:
		applyModelCalled(sess, taskID, ev)
	case protocol.Error:
		if p, err := protocol.Payload[protocol.ErrorPayload](ev); err == nil {
			sess.add(Entry{Kind: ErrorEntry, TaskID: taskID, Text: p.Message})
		}
	case protocol.ModelError:
		if p, err := protocol.Payload[protocol.ModelErrorPayload](ev); err == nil {
			sess.add(Entry{Kind: ErrorEntry, TaskID: taskID, Text: modelErrorText(p)})
			s.setStatus(sess, NeedsYou)
		}
	}
}

func modelErrorText(p protocol.ModelErrorPayload) string {
	switch p.Kind {
	case "key":
		return p.ModelKey + " rejected the API key. Fix it in the vault (ctrl+k), then send any message to retry."
	case "credits":
		return p.ModelKey + " is out of credits. Top up, then send any message to retry."
	case "model":
		return p.ModelKey + " isn't available any more. Send a message to retry, or ctrl+n to start over on another model."
	case "rate_limit":
		return p.ModelKey + " is rate-limited. Wait a little, then send any message to retry."
	default:
		return p.ModelKey + "'s provider is having trouble. Send any message to retry."
	}
}

func (s *Store) applyAttached(ev protocol.Event) {
	p, err := protocol.Payload[protocol.AttachedPayload](ev)
	if err != nil || ev.SessionID == "" {
		return
	}
	view := p.State
	sess := s.ensure(ev.SessionID)
	sess.Task, sess.Workspace, sess.Mode, sess.PermissionMode = sideQuestion(view.Task), view.Workspace, view.Mode, view.PermissionMode
	sess.Todos = view.Todos
	if view.ModelKey != nil {
		sess.Model = *view.ModelKey
	}
	if view.ParentID != nil {
		sess.ParentID = *view.ParentID
	}
	for _, turn := range view.Turns {
		s.Apply(protocol.FromStored(ev.SessionID, turn))
	}
	switch view.Status {
	case "done":
		sess.Status = Done
	case "failed":
		sess.Status = Failed
	}
	sess.Unread = false
}

func (s *Store) applySessionStarted(sess *Session, ev protocol.Event) {
	p, err := protocol.Payload[protocol.SessionStartedPayload](ev)
	if err != nil {
		return
	}
	if p.Task == "" && len(s.pending) > 0 {
		next := s.pending[0]
		s.pending = s.pending[1:]
		p.Task, p.Mode = next.Task, next.Mode
		sess.ParentID = next.ParentID
	}
	if p.ParentID != nil {
		sess.ParentID = *p.ParentID
	}
	if p.ModelKey != nil {
		sess.Model = *p.ModelKey
	}
	if p.Workspace != "" {
		sess.Workspace = p.Workspace
	}
	if p.Mode != "" {
		sess.Mode = p.Mode
	}
	if p.PermissionMode != "" {
		sess.PermissionMode = p.PermissionMode
	}
	p.Task = sideQuestion(p.Task)
	sess.Task = p.Task
	sess.Status = Working
	if p.Task != "" {
		sess.add(Entry{Kind: UserEntry, Text: p.Task})
	}
}

func (s *Store) applySessionEnded(sess *Session, ev protocol.Event) {
	s.removeApprovals(func(a Approval) bool { return a.SessionID == sess.ID })
	p, err := protocol.Payload[protocol.SessionEndedPayload](ev)
	if err == nil && p.Status == "failed" {
		sess.Status = Failed
		sess.add(Entry{Kind: ErrorEntry, Text: "session failed: " + p.FinalText})
		return
	}
	sess.Status = Done
}

func (s *Store) applyAgentTurn(sess *Session, taskID string, ev protocol.Event) {
	p, err := protocol.Payload[protocol.AgentTurnPayload](ev)
	if err != nil {
		return
	}
	if taskID == "" {
		if p.ModelKey != "" {
			sess.Model = p.ModelKey
		}
	} else {
		sess.task(taskID).Model = p.ModelKey
	}
	text := strings.TrimSpace(p.Text)
	if text == "" {
		return
	}
	sess.add(Entry{Kind: AgentEntry, TaskID: taskID, Text: text})
	if taskID == "" {
		s.setStatus(sess, Idle)
	}
}

func (s *Store) settleApprovals(sessionID, taskID string) {
	s.removeApprovals(func(a Approval) bool { return a.SessionID == sessionID && a.TaskID == taskID })
	s.releaseIfClear(sessionID)
}

func applyInjected(sess *Session, taskID string, ev protocol.Event) {
	p, err := protocol.Payload[protocol.HumanMessageInjectedPayload](ev)
	if err != nil {
		return
	}
	content := strings.TrimSpace(p.Content)
	for i := range sess.Entries {
		e := &sess.Entries[i]
		if e.Kind == UserEntry && e.Pending && e.TaskID == taskID && strings.TrimSpace(e.Text) == content {
			e.Pending = false
			return
		}
	}
	sess.add(Entry{Kind: UserEntry, TaskID: taskID, Text: content})
}

func applyTaskStatus(sess *Session, taskID string, ev protocol.Event) {
	p, err := protocol.Payload[protocol.TaskStatusChangedPayload](ev)
	if err != nil || taskID == "" {
		return
	}
	t := sess.task(taskID)
	t.Status = p.Status
	if p.Error != nil {
		t.Error = *p.Error
	}
	if p.Report != nil {
		t.Report = p.Report
	}
	if p.ModelKey != nil {
		t.Model = *p.ModelKey
	}
}

func applyModelCalled(sess *Session, taskID string, ev protocol.Event) {
	p, err := protocol.Payload[protocol.ModelCalledPayload](ev)
	if err != nil {
		return
	}
	sess.InputTokens += p.InputTokens
	sess.OutputTokens += p.OutputTokens
	if p.Cost == nil {
		sess.CostPartial = true
		return
	}
	sess.Cost += *p.Cost
	if taskID != "" {
		sess.task(taskID).Cost += *p.Cost
	}
}
