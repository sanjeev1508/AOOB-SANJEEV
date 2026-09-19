# Astree alarm explorer (UI)

Frontend and API for browsing PVER analysis outputs. Full project docs:
[../README.md](../README.md).

## Run

```powershell
# API
cd UI\backend
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# UI (other terminal)
cd UI
npm install
npm run dev
```

Open the Vite URL (often `http://localhost:5173`). Choose a PVER, then use
**single alarm** or **single PVER** mode and search an alarm order in the top bar.
