# Arabic Medical Glossary API

REST API with built-in web GUI for managing Arabic medical terminology. Built with FastAPI and SQLite (FTS5 full-text search).

## Features

- **15 API Endpoints** — search, CRUD, bulk import, quality checks, multi-format export, file merge
- **Built-in Web GUI** — dark-themed, RTL Arabic support, responsive design
- **FTS5 Full-Text Search** — fast search across English, Arabic, and definitions
- **Statistics Dashboard** — term counts, source/category distribution charts
- **Quality Reports** — automated data quality checks with scoring
- **Multi-Format Export** — CSV, JSON, SQLite downloads
- **File Import** — drag-and-drop JSON/CSV upload with merge support
- **Docker Ready** — single-container deployment on port 7860

## Quick Start

### Local

```bash
pip install -r requirements.txt
python app.py
```

Visit `http://localhost:7860` for the web GUI, or `http://localhost:7860/docs` for the interactive API docs.

### Docker

```bash
docker build -t glossary-api .
docker run -p 7860:7860 -v ./data:/app glossary-api
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Web GUI |
| GET | `/api/health` | Health check |
| GET | `/api/stats` | Database statistics |
| GET | `/api/terms` | List terms (paginated, filterable) |
| GET | `/api/terms/{id}` | Get single term |
| GET | `/api/search` | Search terms (FTS5 or LIKE) |
| POST | `/api/terms` | Create term |
| PUT | `/api/terms/{id}` | Update term |
| DELETE | `/api/terms/{id}` | Delete term |
| POST | `/api/terms/bulk` | Bulk import (JSON array) |
| GET | `/api/sources` | List sources with counts |
| GET | `/api/quality` | Quality report |
| GET | `/api/export/{format}` | Export (csv/json/sqlite) |
| GET | `/api/random` | Random terms |
| POST | `/api/merge` | Merge uploaded file |

## Database

SQLite with FTS5. The database file (`medical_glossary.db`) is created automatically on first run. Schema includes:

- `terms` — main table with English, Arabic, definitions, category, source, type, confidence, notes
- `terms_fts` — FTS5 virtual table for full-text search
- Auto-sync triggers keep FTS index in sync

## License

MIT