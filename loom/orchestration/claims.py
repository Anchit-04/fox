import threading 

def _norm(path:str)->str:
  return path.replace("\\","/").removeprefix("./")

class FileClaims: 
  def __init__(self):
    self._owners:dict[str,str]={}
    self._lock=threading.Lock()
    
  def claim(self,task_id:str,paths:list[str])->list[str]:
    with self._lock:
      taken = [p for p in map(_norm,paths) if self._owners.get(p,task_id)!= task_id]
      if not taken:
        for p in map(_norm,paths):
          self._owners[p] = task_id
      return taken
  def owner(self,path:str)->str|None:
    with self._lock:
      return self._owners.get(_norm(path))
  
  def release(self, task_id:str)->None:
    with self._lock:
      self._owners = {p:t for p, t in self._owners.items() if t!=task_id}
    
      