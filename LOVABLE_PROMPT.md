# WCGT SSC1 — PQ Risk Review Portal
## Lovable Prompt (FastAPI + Postgres Backend)

---

## Overview
Build a full-stack web application for Grant Thornton's Risk & Quality team to automate and review Pre-Qualification (PQ) risk assessments for RFP/tender documents. The app allows users to upload a tender document, automatically runs a risk analysis via a FastAPI backend, and presents the results as an interactive review form. Users can comment on each clause, approve or flag items, and the entire review is tracked per user.

**CRITICAL ARCHITECTURE RULE:**
The frontend connects ONLY to a FastAPI backend at `http://localhost:8000`. There is NO direct database connection from the frontend. No Supabase integration. No Firebase. All auth, data fetching, and writes go exclusively through FastAPI REST endpoints. Use JWT tokens stored in localStorage for session management.

---

## Color Scheme & Visual Design

Match the Grant Thornton "wcgt Lake" intranet portal exactly:
- **Top navbar:** Deep purple/indigo `#2D1B5E`
- **Primary action buttons:** Teal `#00B5A3`
- **Alert/high risk:** Red-pink `#E8003D`
- **Secondary buttons:** Medium purple `#7B5EA7`
- **Page background:** Light lavender-grey `#F0EFF8`
- **Section header bars:** `#2D1B5E` background, white text, slightly rounded corners
- **Body text:** Dark charcoal `#2C2C3A`
- **Cards/panels:** White `#FFFFFF` with `box-shadow: 0 2px 8px rgba(0,0,0,0.08)`
- **Left accent bar on active/expanded sections:** 4px solid teal `#00B5A3` left border
- **Logo area:** "wcgt" in small grey text + "SSC1" in large teal italic bold text (top left)
- **Font:** DM Sans or Plus Jakarta Sans (import from Google Fonts)
- **Input fields:** White background, `#E0DFF0` border, `#2D1B5E` focus ring
- **Risk badge colors:**
  - HIGH RISK: background `#FDECEA`, text `#C0392B`, border `#E74C3C`
  - MEDIUM RISK: background `#FEF9E7`, text `#B7770D`, border `#F39C12`
  - ACCEPTABLE: background `#EAFAF1`, text `#1E8449`, border `#27AE60`
  - NEEDS REVIEW: background `#EBF5FB`, text `#1A5276`, border `#2E86C1`

---

## Authentication

### Login Page `/login`
- Centered card on lavender-grey background
- wcgt SSC1 logo at top
- Email field + Password field
- "Sign In" button — teal, full width
- No registration link (accounts are admin-created only)
- On submit: `POST http://localhost:8000/auth/login` with `{ email, password }`
- Response: `{ access_token, token_type, user: { id, name, email, role } }`
- Store token in localStorage as `ssc1_token`
- Store user object in localStorage as `ssc1_user`
- Redirect to `/dashboard` on success
- Show error message inline if credentials wrong

### Session Management
- On every API request include header: `Authorization: Bearer {token}`
- On app load, call `GET http://localhost:8000/auth/me` to validate token
- If token expired or invalid, clear localStorage and redirect to `/login`
- Logout button in navbar clears localStorage and redirects to `/login`

### Roles
- `reviewer` — can view all RFPs and add comments
- `admin` — can also upload RFPs, trigger analysis, mark reviews complete, manage users

---

## API Integration

Base URL: `http://localhost:8000`

All requests include header: `Authorization: Bearer {token}`

### Auth Endpoints
```
POST   /auth/login              body: { email, password }
                                returns: { access_token, user }

GET    /auth/me                 returns: { id, name, email, role }
```

### RFP Endpoints
```
GET    /rfps                    returns: [ { id, opportunity_name, client_name, bu,
                                            classification, status, created_at,
                                            uploaded_by_name } ]

POST   /rfps/upload             multipart/form-data:
                                  file: <PDF or DOCX>
                                  opportunity_name, client_name, bu,
                                  classification, state, offering, solutions
                                returns: { job_id, rfp_id, status: "queued" }

GET    /rfps/{rfp_id}           returns full RFP details + all clause results
                                (see Response Shape below)

GET    /rfps/{rfp_id}/status    returns: { status: "queued|processing|completed|failed",
                                           progress: 0-100, current_step: "..." }

GET    /rfps/{rfp_id}/download  returns: DOCX file download
```

### Comments Endpoints
```
GET    /rfps/{rfp_id}/comments                      returns all comments for RFP
GET    /rfps/{rfp_id}/comments?clause={clause_type} returns comments for one clause
POST   /rfps/{rfp_id}/comments                      body: { clause_type, comment_text }
                                                    returns: { id, user_name, comment_text, created_at }
DELETE /rfps/{rfp_id}/comments/{comment_id}         admin only
```

### Users Endpoints (Admin only)
```
GET    /users                   returns list of all users
POST   /users                   body: { name, email, password, role }
DELETE /users/{user_id}
```

### Full RFP Response Shape `GET /rfps/{rfp_id}`
```json
{
  "id": 1,
  "opportunity_name": "TA-10648 IND: Preparation of Sustainable Urban Mobility...",
  "client_name": "ADB",
  "bu": "Transformation Consulting",
  "classification": "RFP/RFQ",
  "state": "Odisha",
  "offering": "Energy & Renewables",
  "solutions": "Energy Efficiency & E-Mobility",
  "status": "completed",
  "created_at": "2025-02-11T10:30:00Z",
  "uploaded_by_name": "Prakhar Nigam",
  "clauses": {
    "liability": {
      "clause_text": "The liability of Consultant...",
      "clause_reference": "Clause 4.1",
      "page_no": 12,
      "risk_level": "HIGH",
      "risk_description": "Liability is uncapped. GTBL faces unlimited exposure.",
      "auto_remark": "It is suggested to request the Client that the overall liabilities are capped at the contract value...",
      "needs_exception_approval": true,
      "needs_eqcr": true,
      "deviation_suggested": ""
    },
    "insurance": { ... },
    "scope": { ... },
    "payment": { ... },
    "deliverables": { ... },
    "personnel": { ... },
    "ld": { ... },
    "penalties": { ... },
    "termination": { ... },
    "eligibility": { ... }
  }
}
```

---

## Page Structure

### 1. `/login`
As described in Authentication section above.

---

### 2. `/dashboard`

**Top navbar** (fixed, full width):
- Left: wcgt SSC1 logo
- Right: logged-in user's name + role badge + Logout button

**Page header:**
- Title: "Strategy Steering Council — Bid Qualification Checklist"
- Subtitle: "PQ Risk Review Portal"
- Right side: "Upload New RFP" button (teal, visible to admins only)

**RFP List Table:**
Columns: Opportunity Name | Client | BU | Classification | Date | Status | Actions

- Status badges: "Queued" (grey), "Processing" (amber, animated pulse), "Completed" (green), "Failed" (red)
- Actions column: "Open Review" button (teal outline) → navigates to `/review/{rfp_id}`
- Table rows have hover state (light lavender background)
- Empty state: centered illustration + text "No reviews yet. Upload an RFP to get started."
- Fetch from `GET /rfps` on page load

**Upload Modal** (admin only, opens on "Upload New RFP" click):
- Modal overlay with white card, deep purple header "Upload New RFP"
- Form fields in 2-column grid:
  - Requestor Name (auto-filled with logged-in user's name, read-only)
  - Classification (dropdown: RFP/RFQ, EOI, Tender, Expression of Interest)
  - Opportunity Name (text)
  - Client Name (text)
  - Name of BU (dropdown: Transformation Consulting, Advisory, Tax, Audit, Risk)
  - State (dropdown of Indian states)
  - Offering (text)
  - Solutions (text)
- Drag-and-drop file upload zone below the fields:
  - Dashed border, lavender background
  - "Drag & drop your RFP here or click to browse"
  - Accepts .pdf and .docx only
  - Shows file name + size once selected
- "Start Analysis" button (teal, full width) at bottom
- POST to `POST /rfps/upload` as multipart/form-data
- On success: close modal, show toast "RFP uploaded. Analysis started.", refresh dashboard list

**Processing State:**
- While status is "processing", show an animated progress card in the dashboard row:
  - Steps: "Parsing document" → "Ingesting into vector store" → "Extracting clauses" → "Evaluating risk"
  - Each step has a spinner → checkmark when done
  - Poll `GET /rfps/{rfp_id}/status` every 3 seconds
  - On completion, "Open Review" button becomes active

---

### 3. `/review/:rfp_id` — MAIN REVIEW PAGE

**Top navbar:** Same as dashboard. Add to right side: "Download SSC1 Report" button (teal outline) → calls `GET /rfps/{rfp_id}/download`.

**Opportunity Details Card (collapsible, expanded by default):**
- Deep purple section header bar: "Details of Opportunity" with a collapse arrow
- Inside: 4-column grid of read-only fields styled like the screenshot:
  - Requestor Name, Classification, Enquiry ID, Pipeline ID
  - Name of BU, Opportunity Name, Client Name, International Opportunity (Yes/No radio, read-only)
  - State, Opportunity Type, Offering, Solutions
- Each field: small grey label above, value in styled input box (light background, not editable)
- Fetch from `GET /rfps/{rfp_id}`

**Risk Summary Bar (below opportunity details):**
- Horizontal bar showing counts: 🔴 X High Risk | 🟡 X Medium | 🟢 X Acceptable | 🔵 X Needs Review
- Calculated from the clauses data

**PQ Review Section:**

Header bar: "Risk & Quality Review — Clause Analysis" (deep purple, white text)

Below it: 10 clause accordion rows, stacked vertically, all on one page, no pagination.

**Clause order:**
1. Limitation of Liability
2. Insurance Clause
3. Scope of Work
4. Payment Terms
5. Deliverables
6. Replacement / Substitution of Personnel
7. Liquidated Damages
8. Penalties
9. Termination Rights
10. Eligibility Clause

**Each clause row — COLLAPSED state:**
- White card with subtle shadow
- Left: 4px teal border if expanded, grey if collapsed
- Row layout (horizontal):
  - Clause number (e.g. "01") in large light grey on far left
  - Clause name in bold dark text
  - Clause reference in small grey text below name (e.g. "Clause 4.1 — Page 12")
  - Risk badge (pill) — colour as defined above
  - Comment count badge — grey pill "3 comments"
  - Expand chevron on far right
- Click anywhere on row to expand

**Each clause row — EXPANDED state:**
Card expands to show 2-column layout:

LEFT COLUMN (60% width):

*Original Clause Text*
- Label: "ORIGINAL CLAUSE TEXT" (small caps, grey)
- Scrollable box, max-height 180px, light grey background `#F8F7FC`, monospace-ish font size 12px
- Shows extracted verbatim text

*Clause Reference*
- Small row: "📄 Clause Reference:" followed by the reference string

*Risk Assessment*
- Label: "RISK ASSESSMENT"
- Large risk badge (same colours, bigger than collapsed state)
- Risk description paragraph below badge

RIGHT COLUMN (40% width):

*Auto-Generated R&Q Remark*
- Label: "AUTO-GENERATED REMARK" (small caps, purple)
- Light purple background box `#F3F0FB`, border left 3px `#7B5EA7`
- Remark text inside
- Small "Auto-generated" chip at top right of box

*Flags row (if applicable):*
- "Exception Approval Required" — red pill if true, grey if false
- "EQCR Applicable" — red pill if true, grey if false
- "Deviation Suggested" — amber pill if text exists

*Deviation Language (if exists):*
- Collapsible section "Suggested Deviation Language ▼"
- Shows deviation text in a yellow-tinted box

---

BELOW BOTH COLUMNS — COMMENTS SECTION (full width, always visible):

Section divider line, then label: "COMMENTS" in small caps grey

*Comment feed:*
- Each comment card:
  - Left: Avatar circle (initials of user, deep purple background white text, 32px)
  - Name in bold + role badge (small, outlined) + timestamp in grey (relative: "2 hours ago")
  - Comment text below
  - If current user's comment AND admin: small "Delete" link in grey on hover
- Comments ordered oldest first
- Fetch from `GET /rfps/{rfp_id}/comments?clause={clause_type}`

*Add comment area:*
- Textarea: "Add a comment on this clause..." placeholder
- Light grey background, rounded, 3 rows tall
- "Post Comment" button (teal, right-aligned) below textarea
- POST to `POST /rfps/{rfp_id}/comments` with `{ clause_type, comment_text }`
- On success: append new comment to feed immediately, clear textarea, show brief success flash

---

**Bottom of review page:**
- If status is not "completed": "Mark Review as Complete" button — teal, full width, admin only
  - On click: confirmation dialog "Mark this review as complete? This will lock the document."
  - On confirm: PATCH to `POST /rfps/{rfp_id}/complete`
  - Status changes to "Completed", button replaced with green "✓ Review Completed" badge
- If status is "completed": show locked state — green banner "This review has been marked as complete."

---

## Users Management Page `/users` (Admin only)

Simple table: Name | Email | Role | Date Added | Actions (Delete)
"Add User" button opens modal with Name, Email, Password, Role fields.
POST to `POST /users`

---

## Navigation

Sidebar or top nav with links:
- Dashboard (home icon)
- Users (people icon, admin only)
- Logout

Active link has teal left border and teal text.

---

## General UX Requirements

- Loading skeletons (grey animated blocks) while any API call is in progress
- Toast notifications (bottom right):
  - Green for success
  - Red for errors
  - Include the API error message in red toasts
- All forms validate client-side before submitting
- Confirmation dialogs before destructive actions (delete comment, delete user, mark complete)
- If API returns 401, redirect to `/login` automatically
- If API returns 500, show error toast with "Something went wrong. Please try again."
- All dates displayed in IST (Indian Standard Time) format: "11 Feb 2025, 10:30 AM"
- Responsive layout — desktop first, tablet usable, mobile acceptable

---

## Technology Stack for Frontend

- React + TypeScript
- React Router for navigation
- Axios for API calls (with interceptor to add Bearer token to all requests)
- TailwindCSS for styling
- shadcn/ui for base components (customize colors to match scheme above)
- React Query (TanStack Query) for data fetching, caching, and polling
- Lucide React for icons
- date-fns for date formatting

Do NOT use:
- Supabase client library
- Firebase
- Any direct database connection
- Next.js (use plain React)
