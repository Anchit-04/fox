from dataclasses import dataclass

import permissions
from orchestration.tasks import Lane, Task

READ_LANE_TOOLS: frozenset[str] = (
    frozenset(name for name, (_, modifies) in permissions.PATH_TOOLS.items() if not modifies)
    | frozenset(permissions.INTERNAL_TOOLS)
    | {"run_bash_command"}
)


class PolicyRefusal(Exception):
    pass


@dataclass(frozen=True)
class Admission:
    lane: Lane
    mode: str

    def tools(self, all_tools: list[dict]) -> list[dict]:
        if self.lane is Lane.READ:
            return [t for t in all_tools if t["name"] in READ_LANE_TOOLS]
        return list(all_tools)


def lane_from_proposal(value: str | None) -> Lane:
    return Lane.READ if value == Lane.READ.value else Lane.WRITE


def admit(task: Task, session_mode: str) -> Admission:
    if session_mode not in permissions.MODES:
        raise ValueError(f"unknown session mode {session_mode!r}; expected one of {permissions.MODES}")

    if task.lane is Lane.READ:
        return Admission(lane=Lane.READ, mode=permissions.READ_ONLY)

    if session_mode == permissions.READ_ONLY:
        raise PolicyRefusal(
            f"{task.id} refused: this session is read-only. Re-plan it as a read "
            "task, or ask the user to switch the session to a writable mode."
        )
    return Admission(lane=Lane.WRITE, mode=session_mode)
