# SSC1 PQ Automation — Setup & Usage

## What this does
Upload any RFP/tender (PDF or DOCX) → get back a filled SSC1 Risk & Quality Review DOCX table.

Covers all 10 clause types:
- Limitation of Liability
- Insurance Clause
- Scope of Work
- Payment Terms
- Deliverables
- Replacement/Substitution of Personnel
- Liquidated Damages
- Penalties
- Termination Rights
- Eligibility Clauses (blacklisting / termination / penalty)

---

## Setup (one-time)

### 1. Install Python dependencies
```bash
pip install -r requirements.txt
```

### 2. Install Ollama (free local LLM)
```bash
# Linux/Mac
curl -fsSL https://ollama.com/install.sh | sh

# Windows: download from https://ollama.com/download
```

### 3. Pull the LLM model
```bash
# Recommended (fast, 2GB RAM needed)
ollama pull llama3.2

# Lighter alternative (1.5GB RAM)
ollama pull phi4

# Higher quality (8GB RAM needed)
ollama pull llama3.1:8b
```

### 4. Start Ollama server
```bash
ollama serve
# Runs at http://localhost:11434
```

### 5. Put your SSC1 template in the project folder
```
ssc1_rag/
  document_for_format.docx   ← your blank template
```

---

## Usage

### Option A: Command Line
```bash
python pipeline.py --rfp path/to/tender.pdf --output filled_ssc1.docx
```

With a different model:
```bash
python pipeline.py --rfp rfp.docx --output out.docx --model llama3.1:8b
```

### Option B: REST API
```bash
# Start the API server
uvicorn api:app --reload --port 8000

# Upload an RFP
curl -X POST http://localhost:8000/upload \
  -F "file=@tender.pdf"
# Returns: {"job_id": "abc12345", "status": "queued"}

# Poll for completion
curl http://localhost:8000/status/abc12345

# Download the filled SSC1
curl -O http://localhost:8000/download/abc12345
# Saves: SSC1_Review_abc12345.docx
```

---

## Project Structure
```
ssc1_rag/
├── pipeline.py              ← main orchestrator
├── api.py                   ← FastAPI REST server
├── requirements.txt
├── document_for_format.docx ← your SSC1 blank template (YOU PROVIDE THIS)
│
├── core/
│   ├── parser.py            ← PDF/DOCX → semantic chunks
│   ├── vector_store.py      ← ChromaDB ingest + retrieval
│   └── extractor.py         ← RAG + Ollama extraction per clause
│
├── rules/
│   └── risk_engine.py       ← deterministic GTBL risk thresholds
│
└── output/
    └── writer.py            ← fills the SSC1 DOCX table
```

---

## How it works (data flow)

```
RFP (PDF/DOCX)
    │
    ▼
[parser.py] — splits by section headings, NOT fixed char count
    │  "Clause 4.3 Scope of Work\nThe consultant shall..."
    ▼
[vector_store.py] — embeds with sentence-transformers (local, free)
    │  ChromaDB persisted to ./chroma_db/
    ▼
[extractor.py] — for each of 10 clause types:
    │   1. Multi-query RAG retrieval (3 queries × top-3 = 9 candidates)
    │   2. Ollama LLM extracts structured JSON
    │      (clause text, ref, page, cap%, replacement days, etc.)
    ▼
[risk_engine.py] — pure Python rules, no LLM:
    │   liability cap > contract value → HIGH RISK
    │   LD cap ≥ 20% → HIGH RISK
    │   replacement ≤ 30 days → flag
    │   termination unilateral → HIGH RISK
    │   blacklisting declaration + GTBL history → HIGH RISK
    ▼
[writer.py] — fills document_for_format.docx table
    │   Col 3: extracted clause text + page reference
    │   Col 4: risk level + description (colour-coded)
    │   Col 5: pre-written R&Q remark (from instructions doc)
    ▼
Output: filled_ssc1.docx
```

---

## Changing the LLM model

Edit `core/extractor.py`:
```python
OLLAMA_MODEL = "llama3.2"   # change this
```

Or pass `--model` flag on CLI.

---

## Minimum hardware
- RAM: 4GB (with phi4 model)
- RAM: 8GB (with llama3.2 — recommended)
- CPU: Any modern x86/ARM (no GPU needed)
- Disk: ~3GB for model + dependencies

---

## Limitations & known gaps
1. Page numbers in DOCX are estimated (DOCX doesn't expose real page breaks easily)
2. Very poorly structured RFPs (no headings, scanned PDFs) will have lower extraction accuracy
3. Scope of work high-risk detection is keyword-based; novel phrasings may be missed
4. EQCR 2.0 policy list of high-risk engagements needs to be added manually to `risk_engine.py`

---

## Next steps (after this works)
- Add frontend (drag-drop RFP, download filled DOCX)
- Add PQ marking scheme extraction (turning schemes → scoring tables)
- Add multi-document support (multiple RFPs in one batch)
- Add memory of past RFPs (compare across tenders)
