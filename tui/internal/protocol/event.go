package protocol

import "encoding/json"

const (
	SessionStarted       = "session_started"
	SessionEnded         = "session_ended"
	SessionResumed       = "session_resumed"
	TaskReopened         = "task_reopened"
	AgentTurn            = "agent_turn"
	TodoUpdated          = "todo_updated"
	TaskCreated          = "task_created"
	TaskStatusChanged    = "task_status_changed"
	MemoryWritten        = "memory_written"
	ToolCall             = "tool_call"
	ToolResult           = "tool_result"
	ToolDenied           = "tool_denied"
	TaskResultsDelivered = "task_results_delivered"
	ProposalMade         = "proposal_made"
	ScopeAudit           = "scope_audit"
	HumanMessageInjected = "human_message_injected"
	ApprovalRequested    = "approval_requested"
	Error                = "error"
	ModelError           = "model_error"
	ModelCalled          = "model_called"
	AwaitingInput        = "awaiting_input"

	Attached           = "attached"
	Models             = "models"
	HumanMessageQueued = "human_message_queued"
	EndSessionQueued   = "end_session_queued"
	SessionStatus      = "session_status"
	Projects           = "projects"
	Sessions           = "sessions"
	KillSessionDone    = "kill_session_done"
	SessionRemoved     = "session_removed"
	DeleteSessionDone  = "delete_session_done"
	ArchiveSessionDone = "archive_session_done"
	CleanPreview       = "clean_sessions_preview"
	CleanDone          = "clean_sessions_done"
)

type Event struct {
	Type      string          `json:"type"`
	SessionID string          `json:"session_id"`
	TaskID    *string         `json:"task_id"`
	Seq       int             `json:"seq"`
	Raw       json.RawMessage `json:"-"`
}

func Decode(data []byte) (Event, error) {
	var ev Event
	if err := json.Unmarshal(data, &ev); err != nil {
		return Event{}, err
	}
	ev.Raw = data
	return ev, nil
}

type Stored struct {
	Seq     int             `json:"seq"`
	TS      string          `json:"ts"`
	TaskID  *string         `json:"task_id"`
	Type    string          `json:"type"`
	Payload json.RawMessage `json:"payload"`
}

func FromStored(sessionID string, s Stored) Event {
	return Event{Type: s.Type, SessionID: sessionID, TaskID: s.TaskID, Seq: s.Seq, Raw: s.Payload}
}
func Payload[T any](ev Event) (T, error) {
	var p T
	err := json.Unmarshal(ev.Raw, &p)
	return p, err
}
