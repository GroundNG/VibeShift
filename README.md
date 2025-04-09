# Build challenge by crustdata
### Loom recording:
https://www.loom.com/share/202d2e5ecc294ba49ddbb02b17324f2e?sid=1df03b40-b390-4ebe-9710-598a3de8b885

### Features:
- Independent browser processes
- Header/UA rotation to avoid detection as script
- Vision LLM as fallback to gracefully handle unrecoverable situations
- Can extract data into a json file(for now)
- 4 AI agents working collaboratively - WebAgent, BrowserController, HTMLProcessor, TaskManager

### How to run?
```bash
python -m venv venv
source /venv/bin/activate
pip install -r requirements.txt
fastapi dev server.py
```
