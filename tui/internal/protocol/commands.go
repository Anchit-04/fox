package protocol

import "encoding/json"

type Command interface {
	Kind() string
}

type StartSession struct {
	Task            string `json:"task"`
	ModelKey        string `json:"model_key,omitempty"`
	Mode            string `json:"mode,omitempty"`
	PermissionMode  string `json:"permission_mode,omitempty"`
	ParentSessionID string `json:"parent_session_id,omitempty"`
	Workspace       string `json:"workspace,omitempty"`
	SharedMemory    bool   `json:"shared_memory,omitempty"`
}

type AttachSession struct {
	SessionID string `json:"session_id"`
}

type HumanMessage struct {
	SessionID string  `json:"session_id"`
	TaskID    *string `json:"task_id"`
	Content   string  `json:"content"`
}

type EndSession struct {
	SessionID string  `json:"session_id"`
	TaskID    *string `json:"task_id"`
}

type ApprovalResponse struct {
	SessionID string `json:"session_id"`
	RequestID string `json:"request_id"`
	ToolName  string `json:"tool_name"`
	Approved  bool   `json:"approved"`
	Always    bool   `json:"always"`
}

type ListModels struct {
	Validate bool `json:"validate"`
}

type ListSessions struct {
	IncludeArchived bool `json:"include_archived,omitempty"`
}

type DeleteSession struct {
	SessionID string `json:"session_id"`
}

type ArchiveSession struct {
	SessionID string `json:"session_id"`
	Archived  bool   `json:"archived"`
}

type CleanSessions struct {
	Project string `json:"project,omitempty"`
	DryRun  bool   `json:"dry_run,omitempty"`
}

type ListProjects struct{}

type SetModelKey struct {
	ModelKey string `json:"model_key"`
	Value    string `json:"value"`
}

func (StartSession) Kind() string     { return "start_session" }
func (AttachSession) Kind() string    { return "attach_session" }
func (HumanMessage) Kind() string     { return "human_message" }
func (EndSession) Kind() string       { return "end_session" }
func (ApprovalResponse) Kind() string { return "approval_response" }
func (ListModels) Kind() string       { return "list_models" }
func (SetModelKey) Kind() string      { return "set_model_key" }
func (ListSessions) Kind() string     { return "list_sessions" }
func (ListProjects) Kind() string     { return "list_projects" }
func (DeleteSession) Kind() string    { return "delete_session" }
func (ArchiveSession) Kind() string   { return "archive_session" }
func (CleanSessions) Kind() string    { return "clean_sessions" }

func Encode(c Command) ([]byte, error) {
	body, err := json.Marshal(c)
	if err != nil {
		return nil, err
	}
	var fields map[string]any
	if err := json.Unmarshal(body, &fields); err != nil {
		return nil, err
	}
	fields["type"] = c.Kind()
	return json.Marshal(fields)
}
