import threading
from dataclasses import asdict,dataclass
from agent import ALL_TOOLS,run_agent
from events import emit
from config import routing
from orchestration.claims import FileClaims
from orchestration.executor_tools import EXECUTOR_TOOLS,REPORT_INSTRUCTIONS,ProposalBox,ReportCollector
from orchestration.policy import Admission,PolicyRefusal,admit
from orchestration.tasks import Lane,Task,TaskStatus
from orchestration.scope_audit import ScopeAudit


@dataclass(frozen=True)
class Finished:
  task: Task
  proposals: tuple[str, ...] = ()


class TaskInbox:
  def __init__(self):
    self._done: list[Finished] = []
    self._cond = threading.Condition()

  def put(self, finished: Finished) -> None:
    with self._cond:
      self._done.append(finished)
      self._cond.notify_all()

  def drain(self) -> list[Finished]:
    with self._cond:
      done, self._done = self._done, []
      return done

  def wait(self, timeout: float | None = None) -> list[Finished]:
    with self._cond:
      self._cond.wait_for(lambda: self._done, timeout)
      done, self._done = self._done, []
      return done


def _set_status(task: Task, to: TaskStatus, event_sink,*,report=None, error=None, model_key=None)->None:
  task.transition(to,report=report,error=error)
  emit(event_sink,"task_status_changed",{"task_id":task.id,"status":to.value,"error":error,
                                         "report":asdict(report) if report else None,"model_key":model_key})

def _memory_text(task: Task)->str:
  if task.report is None:
    return f"{task.status.value}:{task.error}"
  r = task.report
  return(
    f"outcome: {r.outcome}\n"
    f"changes: {';'.join(r.changes) or 'none'}\n"
    f"validation: {r.validation}\n"
    f"blockers: {';'.join(r.blockers) or 'none'}"
  )

def _execute(task:Task,admission:Admission,model_key:str,claims:FileClaims,*,mem,event_sink,injection_queue,approver,proposals:ProposalBox)->None:
  collector = ReportCollector()
  audit = ScopeAudit(task,event_sink)
  error = None
  _set_status(task,TaskStatus.RUNNING,event_sink,model_key=model_key)
  try:
    run_agent(
      task.description+REPORT_INSTRUCTIONS,
      verbose = False,
      model_key = model_key,
      interactive = False,
      task_id = task.id,
      event_sink = audit.sink,
      mem = mem,
      injection_queue=injection_queue,
      mode = admission.mode,
      approver = approver,
      tools = admission.tools(ALL_TOOLS+EXECUTOR_TOOLS),
      extra_handlers= {**collector.handlers(),"ask_orchestrator":proposals.handler_for(task.id)},
      stop_when = lambda: collector.done,
      claims = claims,
    )
  except Exception as e:
    error = f"executor crashed: {type(e).__name__};{e}"
  audit.finish()
  if error is None and collector.done:
    _set_status(task,TaskStatus.DONE,event_sink,report=collector.report)
  else:
    _set_status(task,TaskStatus.FAILED,event_sink,error=error or "ended without calling report")


def _run_one(task:Task, session_mode:str,claims:FileClaims,*,mem,event_sink,injection_queue,approver,proposals:ProposalBox)->None:
  model_key= "none"
  try:
    admission = admit(task,session_mode)
    model_key = routing.pick_executor(preferred=task.model_key)
  except(PolicyRefusal,routing.RoutingError) as e:
    _set_status(task,TaskStatus.FAILED,event_sink,error = str(e))
  else:
    _execute(task,admission,model_key,claims,mem=mem,event_sink=event_sink,injection_queue=injection_queue,approver=approver,proposals=proposals)
  mem.append_entry(task.id,model_key,_memory_text(task),scope =task.scope,kind="task_result",task_id=task.id)

def start_task(task:Task,session_mode:str,*,claims:FileClaims,inbox:TaskInbox,mem,event_sink=None,injection_queue=None,approver=None)->list[str]:
  emit(event_sink,"task_created",{"task_id":task.id,"description":task.description,"lane":task.lane.value,"scope":task.scope})
  if task.lane is Lane.WRITE:
    taken = claims.claim(task.id,task.scope)
    if taken:
      _set_status(task,TaskStatus.FAILED,event_sink,error=f"files already claimed by a running task: {', '.join(taken)}")
      return taken
  proposals = ProposalBox(event_sink)

  def run()->None:
    try:
      _run_one(task,session_mode,claims,mem=mem,event_sink=event_sink,injection_queue=injection_queue,approver=approver,proposals=proposals)
    finally:
      if not task.is_terminal:
        _set_status(task,TaskStatus.FAILED,event_sink,error="executor thread ended without a final state")
      claims.release(task.id)
      inbox.put(Finished(task,tuple(p.text for p in proposals.drain())))

  threading.Thread(target=run,daemon=True).start()
  return []
