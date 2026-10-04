import threading 
from contextlib import nullcontext
from agent import ALL_TOOLS,run_agent,_emit
from config import routing 
from orchestration.executor_tools import EXECUTOR_TOOLS,REPORT_INSTRUCTIONS,ReportCollector
from orchestration.policy import Admission,PolicyRefusal,admit
from orchestration.tasks import Task,TaskStatus 
from orchestration.scope_audit import ScopeAudit


def _set_status(task: Task, to: TaskStatus, event_sink,*,report=None, error=None)->None:
  task.transition(to,report=report,error=error)
  _emit(event_sink,"task_status_changed",{"task_id":task.id,"status":to.value,"error":error})
  
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

def _execute(task:Task,admission:Admission,model_key:str,writer_slot:threading.Lock,*,mem,event_sink,injection_queue,approver)->None:
  collector = ReportCollector()
  audit = ScopeAudit(task,event_sink)
  error = None
  slot = writer_slot if admission.needs_writer_slot else nullcontext()
  with slot:
    _set_status(task,TaskStatus.RUNNING,event_sink)
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
        extra_handlers= collector.handlers(),
        stop_when = lambda: collector.done,
      )
    except Exception as e:
      error = f"executor crashed: {type(e).__name__};{e}"
  audit.finish()
  if error is None and collector.done:
    _set_status(task,TaskStatus.DONE,event_sink,report=collector.report)
  else:
    _set_status(task,TaskStatus.FAILED,event_sink,error=error or "ended without calling report")
      
      
def _run_one(task:Task, session_mode:str,writer_slot:threading.Lock,*,mem,event_sink,injection_queue,approver)->None:
  model_key= "none"
  try:
    admission = admit(task,session_mode)
    model_key = routing.pick_executor(preferred=task.model_key)
  except(PolicyRefusal,routing.RoutingError) as e:
    _set_status(task,TaskStatus.FAILED,event_sink,error = str(e))
  else:
    _execute(task,admission,model_key,writer_slot,mem=mem,event_sink=event_sink,injection_queue=injection_queue,approver=approver)
  mem.append_entry(task.id,model_key,_memory_text(task),scope =task.scope,kind="task_result")
  _emit(event_sink,"memory_entry_added",{"task_id":task.id,"agent":model_key})

def run_batch(tasks:list[Task],session_mode:str,*,mem,event_sink=None,injection_queue=None,approver=None)->None:
  writer_slot = threading.Lock()
  for task in tasks:
    _emit(event_sink,"task_created",{"task_id":task.id,"description":task.description,"lane":task.lane.value,"scope":task.scope})
  threads = [ threading.Thread(target=_run_one,args=(task,session_mode,writer_slot),kwargs={"mem":mem,"event_sink":event_sink,"injection_queue":injection_queue,"approver":approver}) for task in tasks]
  for th in threads:
    th.start()
  for th in threads:
    th.join()
  for task in tasks:
    if not task.is_terminal:
      _set_status(task,TaskStatus.FAILED,event_sink,error="executor thread ended without a final state")
    