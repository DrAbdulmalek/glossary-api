"""
Arabic Medical Glossary API
FastAPI application with built-in web GUI for managing medical terminology.
"""

import sqlite3
import csv
import io
import json
import shutil
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, Query, HTTPException, UploadFile, File, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from contextlib import contextmanager

# ─── Configuration ────────────────────────────────────────────────────────────

DB_PATH = Path("medical_glossary.db")
app = FastAPI(
    title="Arabic Medical Glossary API",
    description="REST API for managing Arabic medical terminology with FTS5 search",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Database Manager ─────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    english TEXT NOT NULL,
    arabic TEXT NOT NULL,
    definition_en TEXT,
    definition_ar TEXT,
    category TEXT DEFAULT 'General',
    source TEXT DEFAULT 'Manual',
    term_type TEXT DEFAULT 'general',
    confidence REAL DEFAULT 0.5,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_english ON terms(english);
CREATE INDEX IF NOT EXISTS idx_category ON terms(category);
CREATE INDEX IF NOT EXISTS idx_source ON terms(source);
CREATE INDEX IF NOT EXISTS idx_type ON terms(term_type);
CREATE INDEX IF NOT EXISTS idx_confidence ON terms(confidence);

CREATE VIRTUAL TABLE IF NOT EXISTS terms_fts USING fts5(
    english, arabic, definition_en, definition_ar, category, source,
    content=terms, content_rowid=id
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS terms_ai AFTER INSERT ON terms BEGIN
    INSERT INTO terms_fts(rowid, english, arabic, definition_en, definition_ar, category, source)
    VALUES (new.id, new.english, new.arabic, new.definition_en, new.definition_ar, new.category, new.source);
END;

CREATE TRIGGER IF NOT EXISTS terms_ad AFTER DELETE ON terms BEGIN
    INSERT INTO terms_fts(terms_fts, rowid, english, arabic, definition_en, definition_ar, category, source)
    VALUES ('delete', old.id, old.english, old.arabic, old.definition_en, old.definition_ar, old.category, old.source);
END;

CREATE TRIGGER IF NOT EXISTS terms_au AFTER UPDATE ON terms BEGIN
    INSERT INTO terms_fts(terms_fts, rowid, english, arabic, definition_en, definition_ar, category, source)
    VALUES ('delete', old.id, old.english, old.arabic, old.definition_en, old.definition_ar, old.category, old.source);
    INSERT INTO terms_fts(rowid, english, arabic, definition_en, definition_ar, category, source)
    VALUES (new.id, new.english, new.arabic, new.definition_en, new.definition_ar, new.category, new.source);
END;
"""


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Initialize database with schema."""
    with get_db() as conn:
        conn.executescript(SCHEMA_SQL)


def row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a database row to a dictionary."""
    return dict(row)


# ─── Pydantic Models ──────────────────────────────────────────────────────────

class TermCreate(BaseModel):
    english: str = Field(..., min_length=1, max_length=500)
    arabic: str = Field(..., min_length=1, max_length=500)
    definition_en: Optional[str] = None
    definition_ar: Optional[str] = None
    category: str = "General"
    source: str = "Manual"
    term_type: str = "general"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    notes: Optional[str] = None


class TermUpdate(BaseModel):
    english: Optional[str] = Field(None, min_length=1, max_length=500)
    arabic: Optional[str] = Field(None, min_length=1, max_length=500)
    definition_en: Optional[str] = None
    definition_ar: Optional[str] = None
    category: Optional[str] = None
    source: Optional[str] = None
    term_type: Optional[str] = None
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    notes: Optional[str] = None


class BulkImport(BaseModel):
    terms: List[TermCreate]


# ─── API Endpoints ────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
async def serve_gui():
    return HTML_CONTENT


@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat(), "version": "1.0.0"}


@app.get("/api/stats")
async def get_stats():
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
        sources = conn.execute("SELECT COUNT(DISTINCT source) FROM terms").fetchone()[0]
        categories = conn.execute("SELECT COUNT(DISTINCT category) FROM terms").fetchone()[0]
        avg_conf = conn.execute("SELECT ROUND(AVG(confidence), 3) FROM terms").fetchone()[0] or 0
        types = conn.execute("SELECT COUNT(DISTINCT term_type) FROM terms").fetchone()[0]
        source_dist = dict(
            conn.execute(
                "SELECT source, COUNT(*) as cnt FROM terms GROUP BY source ORDER BY cnt DESC"
            ).fetchall()
        )
        category_dist = dict(
            conn.execute(
                "SELECT category, COUNT(*) as cnt FROM terms GROUP BY category ORDER BY cnt DESC"
            ).fetchall()
        )
    return {
        "total_terms": total,
        "total_sources": sources,
        "total_categories": categories,
        "total_types": types,
        "avg_confidence": avg_conf,
        "source_distribution": source_dist,
        "category_distribution": category_dist,
    }


@app.get("/api/terms")
async def list_terms(
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    category: Optional[str] = None,
    source: Optional[str] = None,
    term_type: Optional[str] = None,
):
    conditions = []
    params = []
    if category:
        conditions.append("category = ?")
        params.append(category)
    if source:
        conditions.append("source = ?")
        params.append(source)
    if term_type:
        conditions.append("term_type = ?")
        params.append(term_type)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM terms{where}", params).fetchone()[0]
        offset = (page - 1) * per_page
        rows = conn.execute(
            f"SELECT * FROM terms{where} ORDER BY english ASC LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()

    return {
        "terms": [row_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    }


@app.get("/api/terms/{term_id}")
async def get_term(term_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM terms WHERE id = ?", (term_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Term not found")
    return row_to_dict(row)


@app.get("/api/search")
async def search_terms(
    q: str = Query("", min_length=0),
    category: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    fulltext: bool = Query(False),
):
    if not q.strip():
        # Return recent terms if no query
        with get_db() as conn:
            conditions = []
            params = []
            if category:
                conditions.append("category = ?")
                params.append(category)
            if source:
                conditions.append("source = ?")
                params.append(source)
            where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
            rows = conn.execute(
                f"SELECT * FROM terms{where} ORDER BY updated_at DESC LIMIT ?",
                params + [limit],
            ).fetchall()
        return {"results": [row_to_dict(r) for r in rows], "total": len(rows), "query": q}

    if fulltext:
        # FTS5 search
        fts_query = re.sub(r'[^\w\s\u0600-\u06FF]', ' ', q).strip()
        fts_query = " OR ".join(fts_query.split())
        sql = """
            SELECT t.* FROM terms t
            JOIN terms_fts fts ON t.id = fts.rowid
            WHERE terms_fts MATCH ?
        """
        params = [fts_query]
    else:
        sql = "SELECT * FROM terms WHERE english LIKE ? OR arabic LIKE ?"
        params = [f"%{q}%", f"%{q}%"]

    if category:
        if fulltext:
            sql += " AND t.category = ?"
        else:
            sql += " AND category = ?"
        params.append(category)
    if source:
        if fulltext:
            sql += " AND t.source = ?"
        else:
            sql += " AND source = ?"
        params.append(source)

    sql += " LIMIT ?"
    params.append(limit)

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    return {"results": [row_to_dict(r) for r in rows], "total": len(rows), "query": q}


@app.post("/api/terms", status_code=201)
async def create_term(term: TermCreate):
    with get_db() as conn:
        try:
            cursor = conn.execute(
                """INSERT INTO terms (english, arabic, definition_en, definition_ar,
                   category, source, term_type, confidence, notes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
                (
                    term.english, term.arabic, term.definition_en, term.definition_ar,
                    term.category, term.source, term.term_type, term.confidence, term.notes,
                ),
            )
            term_id = cursor.lastrowid
            row = conn.execute("SELECT * FROM terms WHERE id = ?", (term_id,)).fetchone()
    return row_to_dict(row)


@app.put("/api/terms/{term_id}")
async def update_term(term_id: int, term: TermUpdate):
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM terms WHERE id = ?", (term_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Term not found")

        updates = []
        params = []
        for field, value in term.model_dump(exclude_unset=True).items():
            updates.append(f"{field} = ?")
            params.append(value)
        if not updates:
            return row_to_dict(existing)

        updates.append("updated_at = datetime('now')")
        params.append(term_id)
        conn.execute(f"UPDATE terms SET {', '.join(updates)} WHERE id = ?", params)
        row = conn.execute("SELECT * FROM terms WHERE id = ?", (term_id,)).fetchone()
    return row_to_dict(row)


@app.delete("/api/terms/{term_id}")
async def delete_term(term_id: int):
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM terms WHERE id = ?", (term_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Term not found")
        conn.execute("DELETE FROM terms WHERE id = ?", (term_id,))
    return {"message": "Term deleted", "id": term_id}


@app.post("/api/terms/bulk", status_code=201)
async def bulk_import(terms: BulkImport):
    added = 0
    skipped = 0
    errors = []
    with get_db() as conn:
        for i, term in enumerate(terms.terms):
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO terms
                       (english, arabic, definition_en, definition_ar,
                        category, source, term_type, confidence, notes,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
                    (
                        term.english, term.arabic, term.definition_en, term.definition_ar,
                        term.category, term.source, term.term_type, term.confidence, term.notes,
                    ),
                )
                if conn.total_changes:
                    added += 1
                else:
                    skipped += 1
            except Exception as e:
                errors.append({"index": i, "term": term.english, "error": str(e)})
    return {"added": added, "skipped": skipped, "errors": errors, "total": len(terms.terms)}


@app.get("/api/sources")
async def list_sources():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT source, COUNT(*) as term_count, ROUND(AVG(confidence),3) as avg_confidence "
            "FROM terms GROUP BY source ORDER BY term_count DESC"
        ).fetchall()
    return {"sources": [row_to_dict(r) for r in rows]}


@app.get("/api/quality")
async def quality_report():
    issues = []
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]

        # Missing Arabic definitions
        missing_ar_def = conn.execute(
            "SELECT COUNT(*) FROM terms WHERE definition_ar IS NULL OR definition_ar = ''"
        ).fetchone()[0]
        if missing_ar_def > 0:
            issues.append({
                "check": "Missing Arabic definitions",
                "count": missing_ar_def,
                "severity": "medium",
                "detail": f"{missing_ar_def} terms lack Arabic definitions",
            })

        # Missing English definitions
        missing_en_def = conn.execute(
            "SELECT COUNT(*) FROM terms WHERE definition_en IS NULL OR definition_en = ''"
        ).fetchone()[0]
        if missing_en_def > 0:
            issues.append({
                "check": "Missing English definitions",
                "count": missing_en_def,
                "severity": "medium",
                "detail": f"{missing_en_def} terms lack English definitions",
            })

        # Low confidence terms
        low_conf = conn.execute(
            "SELECT COUNT(*) FROM terms WHERE confidence < 0.3"
        ).fetchone()[0]
        if low_conf > 0:
            issues.append({
                "check": "Low confidence terms",
                "count": low_conf,
                "severity": "high",
                "detail": f"{low_conf} terms have confidence < 0.3",
            })

        # Duplicate check
        dupes = conn.execute(
            "SELECT COUNT(*) - COUNT(DISTINCT LOWER(english)) FROM terms"
        ).fetchone()[0]
        if dupes > 0:
            issues.append({
                "check": "Potential duplicates",
                "count": dupes,
                "severity": "high",
                "detail": f"{dupes} terms may be duplicates (case-insensitive)",
            })

        # Empty notes
        no_notes = conn.execute(
            "SELECT COUNT(*) FROM terms WHERE notes IS NULL OR notes = ''"
        ).fetchone()[0]

        # Category distribution
        cats = conn.execute(
            "SELECT category, COUNT(*) as c FROM terms GROUP BY category ORDER BY c DESC LIMIT 10"
        ).fetchall()

        # Empty Arabic
        empty_ar = conn.execute(
            "SELECT COUNT(*) FROM terms WHERE arabic IS NULL OR arabic = ''"
        ).fetchone()[0]
        if empty_ar > 0:
            issues.append({
                "check": "Empty Arabic terms",
                "count": empty_ar,
                "severity": "critical",
                "detail": f"{empty_ar} terms have no Arabic translation",
            })

    score = max(0, 100 - len(issues) * 10)
    return {
        "overall_score": score,
        "total_terms": total,
        "issues": issues,
        "issues_count": len(issues),
        "top_categories": [row_to_dict(c) for c in cats],
        "terms_without_notes": no_notes,
    }


@app.get("/api/export/{format}")
async def export_data(format: str):
    if format not in ("csv", "json", "sqlite"):
        raise HTTPException(status_code=400, detail="Format must be csv, json, or sqlite")

    with get_db() as conn:
        rows = conn.execute("SELECT * FROM terms ORDER BY id").fetchall()
        data = [row_to_dict(r) for r in rows]

    if format == "json":
        content = json.dumps(data, ensure_ascii=False, indent=2)
        return StreamingResponse(
            io.StringIO(content),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=glossary_export.json"},
        )

    if format == "csv":
        output = io.StringIO()
        if data:
            writer = csv.DictWriter(output, fieldnames=data[0].keys())
            writer.writeheader()
            writer.writerows(data)
        return StreamingResponse(
            io.StringIO(output.getvalue()),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=glossary_export.csv"},
        )

    if format == "sqlite":
        buf = io.BytesIO()
        tmp = Path("/tmp/export_glossary.db")
        if tmp.exists():
            tmp.unlink()
        src = sqlite3.connect(str(DB_PATH))
        dest = sqlite3.connect(str(tmp))
        src.backup(dest)
        src.close()
        dest.close()
        with open(tmp, "rb") as f:
            buf.write(f.read())
        tmp.unlink()
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/x-sqlite3",
            headers={"Content-Disposition": "attachment; filename=glossary_export.db"},
        )


@app.get("/api/random")
async def random_terms(count: int = Query(5, ge=1, le=50)):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM terms ORDER BY RANDOM() LIMIT ?", (count,)).fetchall()
    return {"terms": [row_to_dict(r) for r in rows], "count": len(rows)}


@app.post("/api/merge")
async def merge_upload(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    content = await file.read()
    added = 0
    skipped = 0
    errors = []

    try:
        if file.filename.endswith(".json"):
            data = json.loads(content)
            if isinstance(data, list):
                terms_data = data
            elif isinstance(data, dict) and "terms" in data:
                terms_data = data["terms"]
            else:
                raise ValueError("Invalid JSON structure")
        elif file.filename.endswith(".csv"):
            text = content.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            terms_data = list(reader)
        else:
            raise HTTPException(status_code=400, detail="Unsupported file format. Use .json or .csv")

        with get_db() as conn:
            for i, item in enumerate(terms_data):
                try:
                    english = str(item.get("english", item.get("English", ""))).strip()
                    arabic = str(item.get("arabic", item.get("Arabic", ""))).strip()
                    if not english or not arabic:
                        skipped += 1
                        continue
                    conn.execute(
                        """INSERT OR IGNORE INTO terms
                           (english, arabic, definition_en, definition_ar,
                            category, source, term_type, confidence, notes,
                            created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
                        (
                            english, arabic,
                            item.get("definition_en", item.get("definition_en", "")),
                            item.get("definition_ar", item.get("definition_ar", "")),
                            item.get("category", "General"),
                            item.get("source", file.filename),
                            item.get("term_type", item.get("type", "general")),
                            float(item.get("confidence", 0.5)),
                            item.get("notes", ""),
                        ),
                    )
                    added += 1
                except Exception as e:
                    errors.append({"index": i, "error": str(e)})
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON file")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "message": f"Merged from {file.filename}",
        "added": added,
        "skipped": skipped,
        "errors": errors,
        "total_processed": added + skipped + len(errors),
    }


# ─── Embedded HTML GUI ────────────────────────────────────────────────────────

HTML_CONTENT = r"""<!DOCTYPE html>
<html lang="en" dir="ltr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Arabic Medical Glossary</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0f172a;--bg2:#1e293b;--bg3:#334155;
  --accent:#14b8a6;--accent2:#0d9488;--accent-glow:rgba(20,184,166,.15);
  --text:#f1f5f9;--text2:#94a3b8;--text3:#64748b;
  --danger:#ef4444;--warning:#f59e0b;--success:#22c55e;
  --radius:10px;--shadow:0 4px 24px rgba(0,0,0,.3);
  --font:'Segoe UI',system-ui,-apple-system,sans-serif;
  --arabic-font:'Noto Sans Arabic','Segoe UI',Tahoma,sans-serif;
}
html{font-size:15px}
body{font-family:var(--font);background:var(--bg);color:var(--text);min-height:100vh;line-height:1.6}
a{color:var(--accent);text-decoration:none}

/* Header */
.header{background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);border-bottom:1px solid var(--bg3);
  padding:1rem 2rem;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;
  backdrop-filter:blur(10px)}
.header h1{font-size:1.4rem;font-weight:700;background:linear-gradient(135deg,var(--accent),#5eead4);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.header .subtitle{color:var(--text2);font-size:.85rem;margin-top:2px}

/* Tabs */
.tabs{display:flex;gap:2px;background:var(--bg2);padding:4px;border-radius:var(--radius);overflow-x:auto}
.tab-btn{padding:.55rem 1.1rem;border:none;background:transparent;color:var(--text2);cursor:pointer;
  border-radius:7px;font-size:.85rem;font-weight:500;white-space:nowrap;transition:all .2s}
.tab-btn:hover{background:var(--bg3);color:var(--text)}
.tab-btn.active{background:var(--accent);color:#fff;font-weight:600}

/* Layout */
.main{padding:1.5rem 2rem;max-width:1400px;margin:0 auto}
.panel{display:none;animation:fadeIn .3s ease}
.panel.active{display:block}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}

/* Cards */
.card{background:var(--bg2);border:1px solid var(--bg3);border-radius:var(--radius);padding:1.25rem;
  box-shadow:var(--shadow)}
.card-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:1rem}
.stat-card{text-align:center;position:relative;overflow:hidden}
.stat-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:var(--accent)}
.stat-card .stat-value{font-size:2.2rem;font-weight:800;color:var(--accent);line-height:1.2}
.stat-card .stat-label{color:var(--text2);font-size:.8rem;text-transform:uppercase;letter-spacing:.5px;margin-top:4px}

/* Search */
.search-box{display:flex;gap:.75rem;align-items:center;flex-wrap:wrap;margin-bottom:1.25rem}
.search-input{flex:1;min-width:250px;padding:.7rem 1rem;background:var(--bg);border:1px solid var(--bg3);
  border-radius:var(--radius);color:var(--text);font-size:.95rem;outline:none;transition:border .2s}
.search-input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-glow)}
.btn{padding:.6rem 1.2rem;border:none;border-radius:7px;cursor:pointer;font-size:.85rem;font-weight:600;
  transition:all .2s;display:inline-flex;align-items:center;gap:6px}
.btn-primary{background:var(--accent);color:#fff}.btn-primary:hover{background:var(--accent2);transform:translateY(-1px)}
.btn-secondary{background:var(--bg3);color:var(--text)}.btn-secondary:hover{background:var(--text3)}
.btn-danger{background:var(--danger);color:#fff}.btn-danger:hover{background:#dc2626}
.btn-sm{padding:.35rem .7rem;font-size:.78rem}
.btn-export{background:var(--bg3);color:var(--text);border:1px solid var(--bg3)}.btn-export:hover{border-color:var(--accent);color:var(--accent)}

/* Filters */
.filters{display:flex;gap:.75rem;flex-wrap:wrap;margin-bottom:1rem;align-items:center}
.filter-select{padding:.5rem .75rem;background:var(--bg);border:1px solid var(--bg3);border-radius:7px;
  color:var(--text);font-size:.85rem;outline:none;min-width:140px}
.filter-select:focus{border-color:var(--accent)}
.filter-label{color:var(--text2);font-size:.8rem;text-transform:uppercase;letter-spacing:.5px}

/* Table */
.table-wrap{overflow-x:auto;border-radius:var(--radius);border:1px solid var(--bg3)}
table{width:100%;border-collapse:collapse;font-size:.88rem}
thead{background:var(--bg3)}
th{padding:.7rem 1rem;text-align:left;color:var(--text2);font-weight:600;font-size:.78rem;
  text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
td{padding:.65rem 1rem;border-top:1px solid var(--bg3);vertical-align:middle}
tr:hover{background:rgba(20,184,166,.05)}
.arabic-cell{direction:rtl;text-align:right;font-family:var(--arabic-font);font-size:1.05rem;line-height:1.8}
.confidence-badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:.75rem;font-weight:600}
.conf-high{background:rgba(34,197,94,.15);color:#4ade80}
.conf-med{background:rgba(245,158,11,.15);color:#fbbf24}
.conf-low{background:rgba(239,68,68,.15);color:#f87171}

/* Pagination */
.pagination{display:flex;align-items:center;justify-content:center;gap:.5rem;margin-top:1.25rem;flex-wrap:wrap}
.page-btn{padding:.4rem .8rem;border:1px solid var(--bg3);background:var(--bg2);color:var(--text);
  border-radius:6px;cursor:pointer;font-size:.82rem;transition:all .2s}
.page-btn:hover,.page-btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.page-info{color:var(--text2);font-size:.82rem;padding:0 .5rem}

/* Bar Chart */
.bar-chart{margin-top:1rem}
.bar-row{display:flex;align-items:center;gap:.75rem;margin-bottom:.5rem}
.bar-label{min-width:140px;text-align:right;color:var(--text2);font-size:.82rem;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.bar-track{flex:1;height:28px;background:var(--bg);border-radius:6px;overflow:hidden;position:relative}
.bar-fill{height:100%;background:linear-gradient(90deg,var(--accent),#5eead4);border-radius:6px;
  transition:width .6s ease;min-width:2px}
.bar-value{position:absolute;right:8px;top:50%;transform:translateY(-50%);font-size:.75rem;
  font-weight:700;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.4)}

/* Form */
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
.form-group{display:flex;flex-direction:column;gap:.3rem}
.form-group.full{grid-column:1/-1}
.form-label{color:var(--text2);font-size:.82rem;font-weight:500}
.form-input,.form-textarea,.form-select{padding:.6rem .8rem;background:var(--bg);border:1px solid var(--bg3);
  border-radius:7px;color:var(--text);font-size:.9rem;outline:none;transition:border .2s}
.form-input:focus,.form-textarea:focus,.form-select:focus{border-color:var(--accent)}
.form-textarea{resize:vertical;min-height:80px;font-family:var(--font)}
.form-input.arabic-input{direction:rtl;text-align:right;font-family:var(--arabic-font);font-size:1rem}

/* Modal */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:200;
  align-items:center;justify-content:center;backdrop-filter:blur(4px)}
.modal-overlay.show{display:flex}
.modal{background:var(--bg2);border:1px solid var(--bg3);border-radius:14px;padding:1.5rem;
  width:90%;max-width:600px;max-height:90vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.modal h2{margin-bottom:1rem;font-size:1.2rem}
.modal-actions{display:flex;gap:.75rem;justify-content:flex-end;margin-top:1.25rem}

/* Quality */
.quality-score{font-size:3.5rem;font-weight:900;text-align:center;margin:1rem 0}
.quality-score.good{color:var(--success)}.quality-score.fair{color:var(--warning)}.quality-score.poor{color:var(--danger)}
.issue-item{display:flex;align-items:center;gap:1rem;padding:.75rem 1rem;background:var(--bg);
  border-radius:8px;margin-bottom:.5rem;border-left:3px solid var(--bg3)}
.issue-item.critical{border-left-color:var(--danger)}
.issue-item.high{border-left-color:var(--warning)}
.issue-item.medium{border-left-color:#3b82f6}
.severity-badge{padding:2px 8px;border-radius:4px;font-size:.7rem;font-weight:700;text-transform:uppercase}
.severity-critical{background:rgba(239,68,68,.15);color:#f87171}
.severity-high{background:rgba(245,158,11,.15);color:#fbbf24}
.severity-medium{background:rgba(59,130,246,.15);color:#60a5fa}

/* Upload */
.upload-zone{border:2px dashed var(--bg3);border-radius:var(--radius);padding:2.5rem;text-align:center;
  cursor:pointer;transition:all .3s}
.upload-zone:hover,.upload-zone.dragover{border-color:var(--accent);background:var(--accent-glow)}
.upload-zone p{color:var(--text2);margin-top:.5rem}

/* Toast */
.toast{position:fixed;bottom:2rem;right:2rem;padding:.8rem 1.2rem;border-radius:var(--radius);
  color:#fff;font-size:.88rem;font-weight:500;z-index:300;transform:translateY(100px);opacity:0;
  transition:all .3s;max-width:400px}
.toast.show{transform:none;opacity:1}
.toast.success{background:var(--success)}.toast.error{background:var(--danger)}.toast.info{background:#3b82f6}

/* Responsive */
@media(max-width:768px){
  .main{padding:1rem}
  .header{padding:1rem}
  .form-grid{grid-template-columns:1fr}
  .card-grid{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
  .search-box{flex-direction:column}
  .search-input{min-width:100%}
  table{font-size:.8rem}
  th,td{padding:.5rem .6rem}
  .modal{width:95%;padding:1rem}
}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>Arabic Medical Glossary</h1>
    <div class="subtitle">REST API & Management System</div>
  </div>
  <div class="tabs">
    <button class="tab-btn active" data-tab="search">Search</button>
    <button class="tab-btn" data-tab="browse">Browse</button>
    <button class="tab-btn" data-tab="add">Add</button>
    <button class="tab-btn" data-tab="stats">Statistics</button>
    <button class="tab-btn" data-tab="quality">Quality</button>
    <button class="tab-btn" data-tab="export">Export</button>
    <button class="tab-btn" data-tab="import">Import</button>
  </div>
</div>

<div class="main">

  <!-- SEARCH PANEL -->
  <div class="panel active" id="panel-search">
    <div class="search-box">
      <input type="text" class="search-input" id="searchInput" placeholder="Search English or Arabic terms..." autofocus>
      <label style="display:flex;align-items:center;gap:6px;color:var(--text2);font-size:.85rem;cursor:pointer">
        <input type="checkbox" id="ftsToggle" checked> FTS5
      </label>
      <button class="btn btn-primary" onclick="doSearch()">Search</button>
    </div>
    <div class="filters">
      <span class="filter-label">Filter:</span>
      <select class="filter-select" id="searchCategory" onchange="doSearch()">
        <option value="">All Categories</option>
      </select>
      <select class="filter-select" id="searchSource" onchange="doSearch()">
        <option value="">All Sources</option>
      </select>
      <span id="searchInfo" style="margin-left:auto;color:var(--text2);font-size:.82rem"></span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>ID</th><th>English</th><th>Arabic</th><th>Category</th><th>Source</th><th>Confidence</th><th>Actions</th>
        </tr></thead>
        <tbody id="searchResults"><tr><td colspan="7" style="text-align:center;color:var(--text3);padding:2rem">
          Type a query and click Search to find terms</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- BROWSE PANEL -->
  <div class="panel" id="panel-browse">
    <div class="search-box">
      <input type="text" class="search-input" id="browseSearch" placeholder="Filter terms...">
    </div>
    <div class="filters">
      <span class="filter-label">Category:</span>
      <select class="filter-select" id="browseCategory" onchange="loadBrowse()"><option value="">All</option></select>
      <span class="filter-label">Source:</span>
      <select class="filter-select" id="browseSource" onchange="loadBrowse()"><option value="">All</option></select>
      <span class="filter-label">Type:</span>
      <select class="filter-select" id="browseType" onchange="loadBrowse()">
        <option value="">All</option><option value="general">General</option><option value="medical">Medical</option>
        <option value="anatomy">Anatomy</option><option value="pharmacology">Pharmacology</option>
        <option value="pathology">Pathology</option><option value="surgical">Surgical</option>
      </select>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>ID</th><th>English</th><th>Arabic</th><th>Category</th><th>Source</th><th>Confidence</th><th>Type</th><th>Actions</th>
        </tr></thead>
        <tbody id="browseResults"></tbody>
      </table>
    </div>
    <div class="pagination" id="browsePagination"></div>
  </div>

  <!-- ADD PANEL -->
  <div class="panel" id="panel-add">
    <div class="card" style="max-width:700px">
      <h2 style="margin-bottom:1.25rem">Add New Term</h2>
      <div class="form-grid">
        <div class="form-group">
          <label class="form-label">English Term *</label>
          <input type="text" class="form-input" id="addEnglish" placeholder="e.g., Hypertension">
        </div>
        <div class="form-group">
          <label class="form-label">Arabic Term *</label>
          <input type="text" class="form-input arabic-input" id="addArabic" placeholder="مثال: ارتفاع ضغط الدم">
        </div>
        <div class="form-group full">
          <label class="form-label">English Definition</label>
          <textarea class="form-textarea" id="addDefEn" placeholder="English definition..."></textarea>
        </div>
        <div class="form-group full">
          <label class="form-label">Arabic Definition</label>
          <textarea class="form-textarea arabic-input" id="addDefAr" placeholder="التعريف باللغة العربية..." style="direction:rtl;text-align:right;font-family:var(--arabic-font)"></textarea>
        </div>
        <div class="form-group">
          <label class="form-label">Category</label>
          <input type="text" class="form-input" id="addCategory" placeholder="General" value="General">
        </div>
        <div class="form-group">
          <label class="form-label">Source</label>
          <input type="text" class="form-input" id="addSource" placeholder="Manual" value="Manual">
        </div>
        <div class="form-group">
          <label class="form-label">Type</label>
          <select class="form-select" id="addType">
            <option value="general">General</option><option value="medical">Medical</option>
            <option value="anatomy">Anatomy</option><option value="pharmacology">Pharmacology</option>
            <option value="pathology">Pathology</option><option value="surgical">Surgical</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Confidence (0-1)</label>
          <input type="number" class="form-input" id="addConfidence" min="0" max="1" step="0.05" value="0.5">
        </div>
        <div class="form-group full">
          <label class="form-label">Notes</label>
          <input type="text" class="form-input" id="addNotes" placeholder="Optional notes...">
        </div>
      </div>
      <div style="margin-top:1.25rem">
        <button class="btn btn-primary" onclick="addTerm()" style="padding:.7rem 2rem">Add Term</button>
      </div>
    </div>
  </div>

  <!-- STATISTICS PANEL -->
  <div class="panel" id="panel-stats">
    <div class="card-grid" id="statCards"></div>
    <div class="card" style="margin-top:1.25rem">
      <h3 style="margin-bottom:.75rem">Source Distribution</h3>
      <div class="bar-chart" id="sourceChart"></div>
    </div>
    <div class="card" style="margin-top:1rem">
      <h3 style="margin-bottom:.75rem">Category Distribution</h3>
      <div class="bar-chart" id="categoryChart"></div>
    </div>
  </div>

  <!-- QUALITY PANEL -->
  <div class="panel" id="panel-quality">
    <div style="text-align:center;margin-bottom:1.5rem">
      <button class="btn btn-primary" onclick="loadQuality()">Run Quality Check</button>
    </div>
    <div id="qualityContent"></div>
  </div>

  <!-- EXPORT PANEL -->
  <div class="panel" id="panel-export">
    <div class="card" style="max-width:600px;text-align:center">
      <h2 style="margin-bottom:1rem">Export Database</h2>
      <p style="color:var(--text2);margin-bottom:1.5rem">Download the entire glossary database in your preferred format.</p>
      <div style="display:flex;gap:1rem;justify-content:center;flex-wrap:wrap">
        <a href="/api/export/csv" class="btn btn-export" style="padding:.8rem 2rem;font-size:1rem">CSV</a>
        <a href="/api/export/json" class="btn btn-export" style="padding:.8rem 2rem;font-size:1rem">JSON</a>
        <a href="/api/export/sqlite" class="btn btn-export" style="padding:.8rem 2rem;font-size:1rem">SQLite</a>
      </div>
    </div>
  </div>

  <!-- IMPORT PANEL -->
  <div class="panel" id="panel-import">
    <div class="card" style="max-width:600px">
      <h2 style="margin-bottom:1rem">Import / Merge Terms</h2>
      <p style="color:var(--text2);margin-bottom:1rem">Upload a JSON or CSV file to merge new terms into the database. Duplicate English terms will be skipped.</p>
      <div class="upload-zone" id="uploadZone" onclick="document.getElementById('fileInput').click()">
        <input type="file" id="fileInput" accept=".json,.csv" style="display:none" onchange="handleUpload(this)">
        <div style="font-size:2.5rem;margin-bottom:.5rem">📂</div>
        <p>Click to upload or drag & drop</p>
        <p style="font-size:.78rem;color:var(--text3)">Supports .json and .csv files</p>
      </div>
      <div id="uploadResult" style="margin-top:1rem"></div>
    </div>
  </div>

</div>

<!-- Edit Modal -->
<div class="modal-overlay" id="editModal">
  <div class="modal">
    <h2>Edit Term #<span id="editId"></span></h2>
    <div class="form-grid">
      <div class="form-group"><label class="form-label">English</label><input type="text" class="form-input" id="editEnglish"></div>
      <div class="form-group"><label class="form-label">Arabic</label><input type="text" class="form-input arabic-input" id="editArabic"></div>
      <div class="form-group full"><label class="form-label">Definition (EN)</label><textarea class="form-textarea" id="editDefEn"></textarea></div>
      <div class="form-group full"><label class="form-label">Definition (AR)</label><textarea class="form-textarea arabic-input" id="editDefAr" style="direction:rtl;text-align:right;font-family:var(--arabic-font)"></textarea></div>
      <div class="form-group"><label class="form-label">Category</label><input type="text" class="form-input" id="editCategory"></div>
      <div class="form-group"><label class="form-label">Source</label><input type="text" class="form-input" id="editSource"></div>
      <div class="form-group"><label class="form-label">Type</label><input type="text" class="form-input" id="editType"></div>
      <div class="form-group"><label class="form-label">Confidence</label><input type="number" class="form-input" id="editConfidence" min="0" max="1" step="0.05"></div>
      <div class="form-group full"><label class="form-label">Notes</label><input type="text" class="form-input" id="editNotes"></div>
    </div>
    <div class="modal-actions">
      <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" onclick="saveEdit()">Save Changes</button>
    </div>
  </div>
</div>

<!-- Toast -->
<div class="toast" id="toast"></div>

<script>
const API = '';
let browsePage = 1;

// ─── Tabs ───
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('panel-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'browse') loadBrowse();
    if (btn.dataset.tab === 'stats') loadStats();
  });
});

// ─── Toast ───
function toast(msg, type='info') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + type + ' show';
  setTimeout(() => t.classList.remove('show'), 3000);
}

// ─── Load Filters ───
async function loadFilters() {
  try {
    const [statsRes, sourcesRes] = await Promise.all([
      fetch(API + '/api/stats'), fetch(API + '/api/sources')
    ]);
    const stats = await statsRes.json();
    const sources = await sourcesRes.json();

    const cats = Object.keys(stats.category_distribution || {});
    ['searchCategory','browseCategory'].forEach(id => {
      const sel = document.getElementById(id);
      const val = sel.value;
      sel.innerHTML = '<option value="">All Categories</option>' +
        cats.map(c => `<option value="${c}">${c}</option>`).join('');
      sel.value = val;
    });
    ['searchSource','browseSource'].forEach(id => {
      const sel = document.getElementById(id);
      const val = sel.value;
      sel.innerHTML = '<option value="">All Sources</option>' +
        sources.sources.map(s => `<option value="${s.source}">${s.source}</option>`).join('');
      sel.value = val;
    });
  } catch(e) { console.error(e); }
}

// ─── Confidence Badge ───
function confBadge(c) {
  c = parseFloat(c);
  const cls = c >= 0.7 ? 'conf-high' : c >= 0.4 ? 'conf-med' : 'conf-low';
  return `<span class="confidence-badge ${cls}">${(c*100).toFixed(0)}%</span>`;
}

// ─── Action Buttons ───
function actionBtns(term) {
  return `<button class="btn btn-sm btn-secondary" onclick='openEdit(${JSON.stringify(term)})'>Edit</button>
          <button class="btn btn-sm btn-danger" onclick="deleteTerm(${term.id})">Del</button>`;
}

// ─── Search ───
async function doSearch() {
  const q = document.getElementById('searchInput').value;
  const fts = document.getElementById('ftsToggle').checked;
  const cat = document.getElementById('searchCategory').value;
  const src = document.getElementById('searchSource').value;
  let url = `${API}/api/search?q=${encodeURIComponent(q)}&fulltext=${fts}&limit=100`;
  if (cat) url += `&category=${encodeURIComponent(cat)}`;
  if (src) url += `&source=${encodeURIComponent(src)}`;
  try {
    const res = await fetch(url);
    const data = await res.json();
    const tbody = document.getElementById('searchResults');
    if (!data.results.length) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text3);padding:2rem">No results found</td></tr>';
    } else {
      tbody.innerHTML = data.results.map(t => `<tr>
        <td>${t.id}</td><td>${esc(t.english)}</td>
        <td class="arabic-cell">${esc(t.arabic)}</td>
        <td>${esc(t.category)}</td><td>${esc(t.source)}</td>
        <td>${confBadge(t.confidence)}</td>
        <td>${actionBtns(t)}</td>
      </tr>`).join('');
    }
    document.getElementById('searchInfo').textContent = `${data.total} result${data.total!==1?'s':''} found`;
  } catch(e) { toast('Search failed: ' + e.message, 'error'); }
}

document.getElementById('searchInput').addEventListener('keydown', e => { if(e.key==='Enter') doSearch(); });

// ─── Browse ───
async function loadBrowse() {
  const cat = document.getElementById('browseCategory').value;
  const src = document.getElementById('browseSource').value;
  const type = document.getElementById('browseType').value;
  let url = `${API}/api/terms?page=${browsePage}&per_page=25`;
  if (cat) url += `&category=${encodeURIComponent(cat)}`;
  if (src) url += `&source=${encodeURIComponent(src)}`;
  if (type) url += `&term_type=${encodeURIComponent(type)}`;
  try {
    const res = await fetch(url);
    const data = await res.json();
    const tbody = document.getElementById('browseResults');
    if (!data.terms.length) {
      tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text3);padding:2rem">No terms found</td></tr>';
    } else {
      tbody.innerHTML = data.terms.map(t => `<tr>
        <td>${t.id}</td><td>${esc(t.english)}</td>
        <td class="arabic-cell">${esc(t.arabic)}</td>
        <td>${esc(t.category)}</td><td>${esc(t.source)}</td>
        <td>${confBadge(t.confidence)}</td><td>${esc(t.term_type||'')}</td>
        <td>${actionBtns(t)}</td>
      </tr>`).join('');
    }
    // Pagination
    const pag = document.getElementById('browsePagination');
    let ph = `<span class="page-info">Page ${data.page} of ${data.pages} (${data.total} terms)</span>`;
    if (data.page > 1) ph += `<button class="page-btn" onclick="browsePage=${data.page-1};loadBrowse()">Prev</button>`;
    const start = Math.max(1, data.page - 2);
    const end = Math.min(data.pages, data.page + 2);
    for (let i = start; i <= end; i++) {
      ph += `<button class="page-btn${i===data.page?' active':''}" onclick="browsePage=${i};loadBrowse()">${i}</button>`;
    }
    if (data.page < data.pages) ph += `<button class="page-btn" onclick="browsePage=${data.page+1};loadBrowse()">Next</button>`;
    pag.innerHTML = ph;
  } catch(e) { toast('Browse failed: ' + e.message, 'error'); }
}

// ─── Add Term ───
async function addTerm() {
  const body = {
    english: document.getElementById('addEnglish').value.trim(),
    arabic: document.getElementById('addArabic').value.trim(),
    definition_en: document.getElementById('addDefEn').value.trim() || null,
    definition_ar: document.getElementById('addDefAr').value.trim() || null,
    category: document.getElementById('addCategory').value.trim() || 'General',
    source: document.getElementById('addSource').value.trim() || 'Manual',
    term_type: document.getElementById('addType').value,
    confidence: parseFloat(document.getElementById('addConfidence').value) || 0.5,
    notes: document.getElementById('addNotes').value.trim() || null,
  };
  if (!body.english || !body.arabic) { toast('English and Arabic terms are required', 'error'); return; }
  try {
    const res = await fetch(API + '/api/terms', {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)
    });
    if (!res.ok) { const err = await res.json(); throw new Error(err.detail || 'Failed'); }
    toast('Term added successfully!', 'success');
    ['addEnglish','addArabic','addDefEn','addDefAr','addNotes'].forEach(id => document.getElementById(id).value='');
    document.getElementById('addConfidence').value = '0.5';
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

// ─── Edit Modal ───
function openEdit(term) {
  document.getElementById('editId').textContent = term.id;
  document.getElementById('editEnglish').value = term.english || '';
  document.getElementById('editArabic').value = term.arabic || '';
  document.getElementById('editDefEn').value = term.definition_en || '';
  document.getElementById('editDefAr').value = term.definition_ar || '';
  document.getElementById('editCategory').value = term.category || '';
  document.getElementById('editSource').value = term.source || '';
  document.getElementById('editType').value = term.term_type || '';
  document.getElementById('editConfidence').value = term.confidence || 0.5;
  document.getElementById('editNotes').value = term.notes || '';
  document.getElementById('editModal').classList.add('show');
}
function closeModal() { document.getElementById('editModal').classList.remove('show'); }
document.getElementById('editModal').addEventListener('click', e => { if(e.target===e.currentTarget) closeModal(); });

async function saveEdit() {
  const id = document.getElementById('editId').textContent;
  const body = {};
  ['editEnglish:english','editArabic:arabic','editDefEn:definition_en','editDefAr:definition_ar',
   'editCategory:category','editSource:source','editType:term_type','editNotes:notes'].forEach(pair => {
    const [elId, key] = pair.split(':');
    const v = document.getElementById(elId).value.trim();
    if (v) body[key] = v;
  });
  body.confidence = parseFloat(document.getElementById('editConfidence').value) || 0.5;
  try {
    const res = await fetch(`${API}/api/terms/${id}`, {
      method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)
    });
    if (!res.ok) throw new Error('Update failed');
    toast('Term updated!', 'success');
    closeModal();
    loadBrowse(); doSearch();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

// ─── Delete ───
async function deleteTerm(id) {
  if (!confirm('Delete term #' + id + '?')) return;
  try {
    const res = await fetch(`${API}/api/terms/${id}`, {method:'DELETE'});
    if (!res.ok) throw new Error('Delete failed');
    toast('Term deleted', 'success');
    loadBrowse(); doSearch();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

// ─── Statistics ───
async function loadStats() {
  try {
    const res = await fetch(API + '/api/stats');
    const s = await res.json();
    document.getElementById('statCards').innerHTML = `
      <div class="card stat-card"><div class="stat-value">${s.total_terms}</div><div class="stat-label">Total Terms</div></div>
      <div class="card stat-card"><div class="stat-value">${s.total_sources}</div><div class="stat-label">Sources</div></div>
      <div class="card stat-card"><div class="stat-value">${s.total_categories}</div><div class="stat-label">Categories</div></div>
      <div class="card stat-card"><div class="stat-value">${s.total_types}</div><div class="stat-label">Types</div></div>
      <div class="card stat-card"><div class="stat-value">${(s.avg_confidence*100).toFixed(1)}%</div><div class="stat-label">Avg Confidence</div></div>
    `;
    const maxSrc = Math.max(...Object.values(s.source_distribution || {0:1}), 1);
    document.getElementById('sourceChart').innerHTML = Object.entries(s.source_distribution || {})
      .map(([k,v]) => `<div class="bar-row"><span class="bar-label" title="${k}">${k}</span>
        <div class="bar-track"><div class="bar-fill" style="width:${(v/maxSrc*100).toFixed(1)}%"><span class="bar-value">${v}</span></div></div></div>`).join('');
    const maxCat = Math.max(...Object.values(s.category_distribution || {0:1}), 1);
    document.getElementById('categoryChart').innerHTML = Object.entries(s.category_distribution || {})
      .map(([k,v]) => `<div class="bar-row"><span class="bar-label" title="${k}">${k}</span>
        <div class="bar-track"><div class="bar-fill" style="width:${(v/maxCat*100).toFixed(1)}%"><span class="bar-value">${v}</span></div></div></div>`).join('');
  } catch(e) { toast('Stats failed: ' + e.message, 'error'); }
}

// ─── Quality ───
async function loadQuality() {
  try {
    const res = await fetch(API + '/api/quality');
    const q = await res.json();
    const cls = q.overall_score >= 80 ? 'good' : q.overall_score >= 50 ? 'fair' : 'poor';
    let html = `<div class="card" style="text-align:center;margin-bottom:1.25rem">
      <div style="color:var(--text2);font-size:.85rem;margin-bottom:.25rem">Quality Score</div>
      <div class="quality-score ${cls}">${q.overall_score}</div>
      <div style="color:var(--text2);font-size:.85rem">${q.total_terms} terms | ${q.issues_count} issues</div>
    </div>`;
    if (q.issues.length) {
      html += '<h3 style="margin-bottom:.75rem">Issues Found</h3>';
      html += q.issues.map(i => `<div class="issue-item ${i.severity}">
        <div style="flex:1"><strong>${esc(i.check)}</strong><br><span style="color:var(--text2);font-size:.85rem">${esc(i.detail)}</span></div>
        <span class="severity-badge severity-${i.severity}">${i.severity}</span>
        <span style="font-weight:700;color:var(--text)">${i.count}</span>
      </div>`).join('');
    } else {
      html += '<div class="card" style="text-align:center;color:var(--success);padding:2rem"><h3>All checks passed!</h3></div>';
    }
    document.getElementById('qualityContent').innerHTML = html;
  } catch(e) { toast('Quality check failed: ' + e.message, 'error'); }
}

// ─── Upload / Import ───
const uploadZone = document.getElementById('uploadZone');
uploadZone.addEventListener('dragover', e => { e.preventDefault(); uploadZone.classList.add('dragover'); });
uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('dragover'));
uploadZone.addEventListener('drop', e => {
  e.preventDefault(); uploadZone.classList.remove('dragover');
  if (e.dataTransfer.files.length) {
    document.getElementById('fileInput').files = e.dataTransfer.files;
    handleUpload(document.getElementById('fileInput'));
  }
});

async function handleUpload(input) {
  if (!input.files.length) return;
  const fd = new FormData();
  fd.append('file', input.files[0]);
  try {
    const res = await fetch(API + '/api/merge', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Upload failed');
    document.getElementById('uploadResult').innerHTML = `
      <div class="card" style="border-left:3px solid var(--success)">
        <h3 style="color:var(--success);margin-bottom:.5rem">Import Complete: ${data.message}</h3>
        <p>Added: <strong style="color:var(--accent)">${data.added}</strong> | 
           Skipped: <strong style="color:var(--warning)">${data.skipped}</strong> | 
           Errors: <strong style="color:var(--danger)">${data.errors.length}</strong></p>
        ${data.errors.length ? '<details style="margin-top:.5rem"><summary style="color:var(--text2);cursor:pointer">View errors</summary><pre style="color:var(--danger);font-size:.8rem;max-height:200px;overflow:auto">' + JSON.stringify(data.errors, null, 2) + '</pre></details>' : ''}
      </div>`;
    toast(`Imported ${data.added} terms`, 'success');
    loadFilters();
  } catch(e) { toast('Upload error: ' + e.message, 'error'); }
  input.value = '';
}

// ─── Utils ───
function esc(s) {
  if (s == null) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// ─── Init ───
loadFilters();
</script>
</body>
</html>"""


# ─── Run directly ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860)