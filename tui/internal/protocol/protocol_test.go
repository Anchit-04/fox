package protocol

import (
	"encoding/json"
	"testing"
)

func encodeToMap(t *testing.T, c Command) map[string]any {
	t.Helper()
	data, err := Encode(c)
	if err != nil {
		t.Fatalf("Encode: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("Encode produced invalid JSON: %v", err)
	}
	return got
}

func TestDecodeLiveFrame(t *testing.T) {
	frame := []byte(`{"session_id":"s1","type":"agent_turn","task_id":null,"model_key":"nemotron-super","text":"Done."}`)
	ev, err := Decode(frame)
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}
	if ev.Type != AgentTurn || ev.SessionID != "s1" || ev.TaskID != nil {
		t.Fatalf("envelope = %+v", ev)
	}
	p, err := Payload[AgentTurnPayload](ev)
	if err != nil {
		t.Fatalf("Payload: %v", err)
	}
	if p.ModelKey != "nemotron-super" || p.Text != "Done." {
		t.Fatalf("payload = %+v", p)
	}
}

func TestDecodeExecutorTaskID(t *testing.T) {
	ev, err := Decode([]byte(`{"session_id":"s1","type":"tool_call","task_id":"t1","name":"read_file","args":{"path":"a.py"}}`))
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}
	if ev.TaskID == nil || *ev.TaskID != "t1" {
		t.Fatalf("TaskID = %v", ev.TaskID)
	}
}

func TestFromStoredMatchesLive(t *testing.T) {
	stored := Stored{
		Seq:     4,
		TS:      "2026-10-30T10:00:00.000Z",
		Type:    AgentTurn,
		Payload: json.RawMessage(`{"task_id":null,"model_key":"nemotron-super","text":"Done."}`),
	}
	ev := FromStored("s1", stored)
	if ev.SessionID != "s1" || ev.Seq != 4 || ev.Type != AgentTurn {
		t.Fatalf("envelope = %+v", ev)
	}
	p, err := Payload[AgentTurnPayload](ev)
	if err != nil {
		t.Fatalf("Payload: %v", err)
	}
	if p.Text != "Done." {
		t.Fatalf("payload = %+v", p)
	}
}

func TestCostNullVersusFree(t *testing.T) {
	unknown, _ := Decode([]byte(`{"type":"model_called","model_key":"x","cost":null}`))
	free, _ := Decode([]byte(`{"type":"model_called","model_key":"x","cost":0}`))

	u, err := Payload[ModelCalledPayload](unknown)
	if err != nil || u.Cost != nil {
		t.Fatalf("unknown price: cost = %v, err = %v", u.Cost, err)
	}
	f, err := Payload[ModelCalledPayload](free)
	if err != nil || f.Cost == nil || *f.Cost != 0 {
		t.Fatalf("free model: cost = %v, err = %v", f.Cost, err)
	}
}

func TestEncodeAddsType(t *testing.T) {
	got := encodeToMap(t, HumanMessage{SessionID: "s1", Content: "hi"})
	if got["type"] != "human_message" {
		t.Fatalf("type = %v", got["type"])
	}
	v, ok := got["task_id"]
	if !ok || v != nil {
		t.Fatalf("task_id should be present and null, got %v (present: %v)", v, ok)
	}
}

func TestEncodeOmitsUnsetStartFields(t *testing.T) {
	got := encodeToMap(t, StartSession{Task: "x"})
	for _, key := range []string{"model_key", "mode", "permission_mode", "parent_session_id"} {
		if _, ok := got[key]; ok {
			t.Errorf("%s should be omitted, got %v", key, got[key])
		}
	}
	if got["task"] != "x" {
		t.Fatalf("task = %v", got["task"])
	}
}

func TestEncodeKeepsDenial(t *testing.T) {
	got := encodeToMap(t, ApprovalResponse{SessionID: "s1", RequestID: "approval_1", Approved: false})
	if v, ok := got["approved"]; !ok || v != false {
		t.Fatalf("approved should be present and false, got %v (present: %v)", v, ok)
	}
}

func TestStartSessionCarriesWorkspaceAndSharedMemory(t *testing.T) {
	got := encodeToMap(t, StartSession{Task: "fix it", Workspace: `C:\work\api`, SharedMemory: true})
	if got["type"] != "start_session" || got["workspace"] != `C:\work\api` || got["shared_memory"] != true {
		t.Fatalf("encoded = %v", got)
	}
	plain := encodeToMap(t, StartSession{Task: "fix it"})
	if _, ok := plain["workspace"]; ok {
		t.Fatalf("empty workspace was sent: %v", plain)
	}
	if _, ok := plain["shared_memory"]; ok {
		t.Fatalf("private chat sent shared_memory: %v", plain)
	}
}

func TestProjectSpaces(t *testing.T) {
	shared, private, empty := "project:c:/work/api", "a3f1-uuid", ""
	if !IsProjectSpace(&shared) || IsProjectSpace(&private) || IsProjectSpace(&empty) || IsProjectSpace(nil) {
		t.Fatal("IsProjectSpace misread a space id")
	}
}
