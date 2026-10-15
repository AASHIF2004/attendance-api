# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in .env
python sample_seed.py                                   # optional: load the 9 sample docs
uvicorn app.main:app --port 8000
```
MongoDB 6.0+ required (uses `$setWindowFields`, `$densify`). Indexes are created at startup. Docs at `/docs`.

## Notes
- Part A findings: `REVIEW.md`. Part C answers: `DECISIONS.md`.
- Everything in the contract is implemented, including `/admin/explain/{endpoint}`.
- Testing caveat: the sandbox where this was written had no MongoDB server, so the CRUD endpoints and rule helpers were
  tested (with `mongomock`), but the aggregation pipelines were not executed against a real server. Please run them on MongoDB 6/7.
