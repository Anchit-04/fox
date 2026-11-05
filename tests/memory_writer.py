import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import memory
from store import Store


def main() -> None:
    db, session_id, workspace, count, go = sys.argv[1:6]
    store = Store.open(db, migrate=False)
    mem = memory.Memory.for_chat(store, session_id, workspace, shared=True)
    while not Path(go).exists():
        pass
    ids = [mem.add_note(f"writer-{session_id}", f"note {i}") for i in range(int(count))]
    print(json.dumps(ids))
    store.close()


if __name__ == "__main__":
    main()
