# SadSha Build Pro for Revit (pyRevit extension)

A free, open **Revit → SadSha Build Pro** bridge — publish the active Revit model into SadSha Build Pro in one click,
**without the paid Autodesk APS bridge**. It uses Revit's built-in IFC exporter and SadSha Build Pro's REST
API, so your model flows straight into the SadSha Build Pro BIM viewer / GC portal / proforma.

Built on the [pyRevit StarterKit](https://www.learnrevitapi.com/) conventions, so it's familiar to the
LearnRevitAPI community.

## What's in the `SadSha Build Pro` tab

| Button | What it does |
|--------|--------------|
| **Publish to SadSha Build Pro** | Exports the active document to IFC (built-in exporter), uploads it, runs the server-side Fragments conversion, then offers to open it in the web viewer. Re-publishing the same project name updates it. |
| **Open in SadSha Build Pro** | Opens the current model's SadSha Build Pro project in the web viewer. |
| **Sync Issues (BCF)** | Round-trips RFIs / clashes / punch pins with SadSha Build Pro over the open **BCF** standard (download a `.bcfzip` or upload one). Issues are keyed by IFC GlobalId, so pins land on the right elements both ways. |
| **Settings** | Stores the SadSha Build Pro **API URL**, **web app URL**, **API key**, and a default project name (per-user, in pyRevit config). |

## Install

1. Install [pyRevit](https://github.com/eirannejad/pyRevit) (free).
2. Register this extension folder:
   ```
   pyrevit extend ui SadSha Build Pro "<path-to>/integrations/pyrevit/SadSha Build Pro.extension"
   ```
   …or drop `SadSha Build Pro.extension/` into a folder on your pyRevit *Extensions* search path and reload.
3. Reload pyRevit → a **SadSha Build Pro** tab appears.

## Configure

Click **SadSha Build Pro ▸ Settings** and enter:
- **API URL** — your SadSha Build Pro API, e.g. `https://your-host/api` (or `https://massing.build/api`).
- **App URL** — the web app origin for "Open in SadSha Build Pro" links, e.g. `https://massing.build`.
- **API key** — a SadSha Build Pro bearer token. **REST API access is a Commercial-plan (and up) entitlement**
  — see Settings ▸ SadSha Build Pro licence in the web app, or massing.cloud/docs.

The manual path (export IFC from Revit → upload in the web app) stays free on any plan; this one-click
bridge uses the API and therefore needs a Commercial licence.

## Notes
- Engine-agnostic: works on pyRevit's IronPython 2.7 and CPython 3 engines (std-lib only — no `requests`).
- The bridge never reads `.rvt` directly; it exports IFC via Revit, honoring SadSha Build Pro's "IFC is the
  source of truth" model.
- Client code: `SadSha Build Pro.extension/lib/massing_api.py` (covered by `services/api/test_revit_bridge.py`).
