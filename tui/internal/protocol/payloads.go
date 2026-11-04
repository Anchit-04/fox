package protocol

type SessionStartedPayload struct {
	Task           string  `json:"task"`
	Workspace      string  `json:"workspace"`
	Mode           string  `json:"mode"`
	PermissionMode string  `json:"permission_mode"`
	ModelKey       *string `json:"model_key"`
	ParentID       *string `json:"parent_id"`
}

type SessionEndedPayload struct {
	Status    string `json:"status"`
	FinalText string `json:"final_text"`
}

type AgentTurnPayload struct {
	ModelKey string `json:"model_key"`
	Text     string `json:"text"`
}

type HumanMessageInjectedPayload struct {
	Content string `json:"content"`
}

type ErrorPayload struct {
	Message string `json:"message"`
}

type ModelErrorPayload struct {
	ModelKey string `json:"model_key"`
	Kind     string `json:"kind"`
	Message  string `json:"message"`
}

type ToolCallPayload struct {
	Name string         `json:"name"`
	Args map[string]any `json:"args"`
}

type ToolResultPayload struct {
	Name    string `json:"name"`
	Content string `json:"content"`
}

type ToolDeniedPayload struct {
	Name   string `json:"name"`
	Reason string `json:"reason"`
}

type ApprovalRequestedPayload struct {
	RequestID   string         `json:"request_id"`
	ToolName    string         `json:"tool_name"`
	Args        map[string]any `json:"args"`
	Reason      string         `json:"reason"`
	Description string         `json:"description"`
}

type TodoUpdatedPayload struct {
	Checklist string `json:"checklist"`
}
type TaskCreatedPayload struct {
	Description string   `json:"description"`
	Lane        string   `json:"lane"`
	Scope       []string `json:"scope"`
}

type Report struct {
	Outcome    string   `json:"outcome"`
	Changes    []string `json:"changes"`
	Validation string   `json:"validation"`
	Blockers   []string `json:"blockers"`
}

type TaskStatusChangedPayload struct {
	Status   string  `json:"status"`
	Error    *string `json:"error"`
	Report   *Report `json:"report"`
	ModelKey *string `json:"model_key"`
}

type TaskResultsDeliveredPayload struct {
	Content string `json:"content"`
}

type ProposalMadePayload struct {
	Text string `json:"text"`
}

type ScopeAuditPayload struct {
	Lane         string   `json:"lane"`
	Declared     []string `json:"declared"`
	Written      []string `json:"written"`
	Undeclared   []string `json:"undeclared"`
	Unused       []string `json:"unused"`
	Mispredicted bool     `json:"mispredicted"`
}

type MemoryWrittenPayload struct {
	EntryID string   `json:"entry_id"`
	Agent   string   `json:"agent"`
	Kind    string   `json:"kind"`
	Scope   []string `json:"scope"`
	Content string   `json:"content"`
}

type ModelCalledPayload struct {
	ModelKey         string   `json:"model_key"`
	DurationMs       int      `json:"duration_ms"`
	InputTokens      int      `json:"input_tokens"`
	OutputTokens     int      `json:"output_tokens"`
	CacheReadTokens  int      `json:"cache_read_tokens"`
	CacheWriteTokens int      `json:"cache_write_tokens"`
	Cost             *float64 `json:"cost"`
	CostSource       *string  `json:"cost_source"`
}

type SessionView struct {
	Task           string   `json:"task"`
	Workspace      string   `json:"workspace"`
	Mode           string   `json:"mode"`
	PermissionMode string   `json:"permission_mode"`
	ModelKey       *string  `json:"model_key"`
	ParentID       *string  `json:"parent_id"`
	Status         string   `json:"status"`
	FinalText      *string  `json:"final_text"`
	Todos          string   `json:"todos"`
	StartedAt      string   `json:"started_at"`
	EndedAt        *string  `json:"ended_at"`
	Turns          []Stored `json:"turns"`
}

type AttachedPayload struct {
	State SessionView `json:"state"`
}

type VaultEntry struct {
	ModelKey  string `json:"model_key"`
	Tier      string `json:"tier"`
	Present   bool   `json:"present"`
	Validated *bool  `json:"validated"`
}

type ModelsPayload struct {
	Models []VaultEntry `json:"models"`
}

type SessionSummary struct {
	SessionID      string  `json:"session_id"`
	ParentID       *string `json:"parent_id"`
	Workspace      string  `json:"workspace"`
	Task           string  `json:"task"`
	Mode           string  `json:"mode"`
	PermissionMode string  `json:"permission_mode"`
	ModelKey       *string `json:"model_key"`
	Status         string  `json:"status"`
}

type SessionsPayload struct {
	Sessions []SessionSummary `json:"sessions"`
}
