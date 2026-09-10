# Astree alarm explorer

## Run locally

From `UI/backend`, install the Python dependencies and start the API:

```powershell
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

From `UI`, install the frontend dependencies and start Vite:

```powershell
npm install
npm run dev
```

The backend defaults to `..\Full_alarms.csv` and
`..\array_oob_variable_info.json` relative to this directory. Override
them with `ASTREE_ALARMS_PATH` and `ASTREE_VARIABLE_INFO_PATH`. Set
`ASTREE_SOURCE_PATH` to change the C source used for function snippets.
Set `ASTREE_CORS_ORIGINS` to a comma-separated list when serving the UI from
a different origin.

`GET /api/alarms` returns the index as `{ "items": [], "total": n }`.
`GET /api/alarm/{order_id}` returns `{ "alarm": {...}, "group": {...} }`
plus flattened compatibility fields used by the UI. The CSV `Location` must
match a JSON group exactly; otherwise the endpoint returns `404`. Order IDs
may be supplied with or without Astree's thousands separators.
