from sandbox import sandbox_client

START_PROCESS_TOOL = {
    "type": "function",
    "name": "start_process",
    "description": (
        "Start a cmd.exe command that keeps running after this call returns: a dev server, a file "
        "watcher, a screen recorder. Returns its process id (like p1), whether it is still running, and "
        "what it printed in its first 2 seconds. Use run_bash_command for commands that finish on their "
        f"own. At most {sandbox_client.MAX_BACKGROUND_PROCESSES} run at once in a session."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The cmd.exe command to start."},
        },
        "required": ["command"],
    },
}

READ_PROCESS_TOOL = {
    "type": "function",
    "name": "read_process",
    "description": (
        "Get what a background process printed since it was last read, and whether it is still running "
        "or has exited (with its exit code). An exited process is reported once, then forgotten. Leave "
        "process empty to list every background process in this session."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "process": {"type": "string", "description": "The process id from start_process, like p1."},
        },
    },
}

STOP_PROCESS_TOOL = {
    "type": "function",
    "name": "stop_process",
    "description": "Stop a background process and everything it started, and return its last output.",
    "parameters": {
        "type": "object",
        "properties": {
            "process": {"type": "string", "description": "The process id from start_process, like p1."},
        },
        "required": ["process"],
    },
}

PROCESS_TOOLS = [START_PROCESS_TOOL, READ_PROCESS_TOOL, STOP_PROCESS_TOOL]

PROCESS_TOOL_HANDLERS = {
    "start_process": lambda args: sandbox_client.start_process(args["command"]),
    "read_process": lambda args: sandbox_client.read_process(args.get("process") or ""),
    "stop_process": lambda args: sandbox_client.stop_process(args["process"]),
}
