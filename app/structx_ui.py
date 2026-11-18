"""Builds the React app's JSON (Asset with its assessment workspace, SourceDocument) from structx.sqlite only.

Read-only. Everything shown comes from these tables: asset, indication, document(+_file_type), input, input_value,
user, user_asset. Nothing is invented: what structx does not hold (revenue, policy impact, eligibility, exposure) is left
at the neutral defaults the UI already uses for a new asset ("—", 0, "Review pending"); the UI engine computes those later.

IDs: the asset id the UI uses is the asset code (structx `asset.name`, unique), indications are "IND-<indication_id>".
"""
import sqlite3
from datetime import datetime

from . import structx_repo as repo
from .structx_map import INPUTS, to_ui
from .security import CurrentUser

POLICIES = ('IRA', 'MFN', 'JCA', 'EU-GPL')


def _label(iso: str | None) -> str:
    """'2026-11-11T09:26:34+00:00' -> '11 Nov 2026'."""
    try:
        return datetime.fromisoformat(str(iso)).strftime('%d %b %Y')
    except ValueError:
        return '—'


def _size(n: int) -> str:
    return f'{n / (1024 * 1024):.1f} MB' if n >= 1024 * 1024 else f'{max(1, round(n / 1024))} KB'


# ---------------- documents

def _doc(asset_code: str, d: dict) -> dict:
    return {
        'id': d['documentId'], 'assetId': asset_code, 'name': d['fileName'], 'size': _size(d['sizeBytes']),
        'type': d['primaryType'], 'types': d['types'], 'scope': 'Program-wide', 'version': 'Latest', 'tag': '',
        'pages': '—', 'added': _label(d['uploadedAt']),
    }


def ui_documents(conn: sqlite3.Connection, user: CurrentUser) -> list[dict]:
    """The UI's SourceDocument list for every asset the user may open."""
    return [_doc(a['name'], d) for a in repo.list_assets(conn, user) for d in repo.list_documents(conn, a['assetId'])]


def ui_asset_documents(conn: sqlite3.Connection, asset: sqlite3.Row) -> list[dict]:
    """The documents of one asset as the UI reads them (the caller has already checked access)."""
    return [_doc(asset['name'], d) for d in repo.list_documents(conn, asset['asset_id'])]


def ui_document(conn: sqlite3.Connection, document_id: int) -> dict:
    """One document as the UI reads it (the caller has already checked access)."""
    row = conn.execute('SELECT d.asset_id, a.name FROM document d JOIN asset a ON a.asset_id = d.asset_id WHERE d.document_id = ?',
                       (document_id,)).fetchone()
    return _doc(row['name'], next(d for d in repo.list_documents(conn, row['asset_id']) if d['documentId'] == document_id))


# ---------------- assets

def _users(conn: sqlite3.Connection) -> dict[int, str]:
    return {r['user_id']: r['name'] for r in conn.execute('SELECT user_id, name FROM "user"')}


def _workspace(conn: sqlite3.Connection, asset: sqlite3.Row, extracted: bool) -> tuple[dict, set[str], str]:
    """The assessment workspace data the UI persists (set-up, indications and catalogue values). View state and defaults
    (variables, policy switches, …) are filled in by the React app when it loads the asset."""
    asset_id = asset['asset_id']
    inds: dict[int, dict] = {}
    for r in repo.list_indications(conn, asset_id):
        inds[r['indicationId']] = {
            'id': f"IND-{r['indicationId']}", 'name': r['indication'], 'orphanUS': '', 'orphanEU': '', 'launchUS': '', 'launchUSMonth': '', 'launchEU': '',
            'include': r['include'], 'source': 'CDP' if r['source'] == 'cdp' else 'Manual', 'v6': {}}
    asset_v6: dict = {}
    comps: dict[tuple, dict] = {}
    seen: set[str] = set()  # input ids that have a value (used to derive jurisdictions)

    rows = conn.execute(
        'SELECT v.indication_id, v.competitor, v.value, i.id AS input_id, i.kind FROM input_value v '
        'JOIN input i ON i.input_id = v.input_id WHERE v.asset_id = ?', (asset_id,)).fetchall()
    for r in rows:
        key, kind, val = r['input_id'], r['kind'], repo._json(r['value'])
        seen.add(key)
        where, ui_key = INPUTS.get(key, (None, None))
        if where == 'asset':
            asset_v6[ui_key] = to_ui(kind, val)
        elif where == 'comp' and r['indication_id'] in inds and r['competitor']:
            c = comps.setdefault((r['indication_id'], r['competitor']), {'name': r['competitor'], 'vsId': inds[r['indication_id']]['id']})
            c[ui_key] = to_ui(kind, val)
        elif where == 'ind' and r['indication_id'] in inds:
            ind = inds[r['indication_id']]
            ui_val = to_ui(kind, val)
            if ui_key == 'usLaunch' and isinstance(ui_val, dict):
                ind['launchUS'], ind['launchUSMonth'] = ui_val['y'], ui_val['m']
            elif ui_key == 'euLaunch':
                ind['launchEU'] = str(ui_val)
            elif ui_key == 'fdaOdd':
                ind['orphanUS'] = str(val)
            elif ui_key == 'emaOd':
                ind['v6']['emaOd'] = str(val)
                ind['orphanEU'] = 'No' if val == 'No' else 'Yes'
            else:
                ind['v6'][ui_key] = ui_val

    modality = asset_v6.get('modality') if isinstance(asset_v6.get('modality'), str) else ''
    workspace = {
        'created': True,  # extracted from documents: required inputs gate Eligibility, Impact and Scenario, as for a new product
        'unlocked': ['submission', 'governance'] if extracted else ['submission'],
        'setup': {'name': asset['asset_name'] or asset['name'], 'stage': asset['stage'] or '',
                  'modality': modality, 'route': '', 'tclass': '', 'indications': list(inds.values()), 'indSeq': max(inds, default=0)},
        'base': {'peakUS': 0, 'peakEU': 0}, 'priority': 62, 'confirmedBy': '', 'confirmedAt': '',
        'v6': {'asset': asset_v6, 'comp': list(comps.values())},
    }
    return workspace, seen


_PHASE_ORDER = ['Pre-clinical', 'Phase I', 'Phase II', 'Phase II/III', 'Phase III']


def _stage(asset_stage: str | None, indications: list[dict]) -> str:
    """The asset's stage. structx may hold none, so the Overview shows the most advanced phase of the included indications."""
    if asset_stage:
        return asset_stage
    phases = [i['v6'].get('phase') for i in indications if i['include'] and i['v6'].get('phase') in _PHASE_ORDER]
    return max(phases, key=_PHASE_ORDER.index) if phases else ''


def _asset(conn: sqlite3.Connection, row: sqlite3.Row, names: dict[int, str]) -> dict:
    extracted = bool(row['extracted_at'])
    cov = repo.coverage(conn, row['asset_id'])
    required = sum(s['required'] for s in cov.values())
    filled = sum(s['filled'] for s in cov.values())
    pct = round(100 * filled / required) if required else 0
    workspace, seen = _workspace(conn, row, extracted)
    has_docs = conn.execute('SELECT 1 FROM document WHERE asset_id = ? LIMIT 1', (row['asset_id'],)).fetchone() is not None
    updated = _label(row['updated_at'] or row['extracted_at'] or row['created_at'])
    owner = names.get(row['owner_user_id']) or names.get(row['created_by']) or '—'
    return {
        'id': row['name'], 'assetCode': row['name'], 'assetName': row['asset_name'] or row['name'],
        'stage': _stage(row['stage'], workspace['setup']['indications']),
        # Derived only from extracted launch dates; policy exposure is computed by the UI engine, not stored in structx.
        'jurisdictions': [j for j, k in (('US', 'us_launch_timing'), ('EU', 'eu_launch_timing')) if k in seen],
        'policyExposures': [],
        'submissionStatus': 'In progress' if not extracted else 'Complete' if required and filled == required else 'Needs review',
        'submissionCompleteness': pct, 'eligibilityStatus': 'Review pending', 'businessContextStatus': 'Draft', 'impactStatus': '—',
        'owner': owner, 'lastUpdatedAt': updated, 'estimatedImpactLevel': '—', 'estimatedAtRisk': 0, 'estimatedOpportunity': 0,
        'recommendationStatus': '—', 'governanceAlerts': 0,
        'summaryMetrics': {'gross': 0, 'net': 0, 'usRevenue': 0, 'euRevenue': 0},
        'eligibility': {p: 'Review pending' for p in POLICIES},
        'context': {'modality': workspace['setup']['modality']} if workspace['setup']['modality'] else {},
        'policyImpact': {p: 0 for p in POLICIES}, 'recommendationCount': 0, 'approvalCount': 0,
        # No extraction_run table yet: "extracting" only when files are attached but never extracted.
        'workflowStatus': 'inputs_review' if extracted else 'extracting' if has_docs else 'active',
        'assessments': [{'id': 'AS-1', 'date': updated, 'by': owner, 'status': 'Current', 'level': '—', 'atRisk': 0, 'completeness': pct}],
        'aia': workspace,
    }


def ui_asset(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """One asset as the UI reads it (the caller has already checked access)."""
    return _asset(conn, row, _users(conn))


def ui_assets(conn: sqlite3.Connection, user: CurrentUser) -> list[dict]:
    """The UI's Asset list (with workspace) for every asset the user may open."""
    ids = repo.accessible_asset_ids(conn, user)
    rows = conn.execute('SELECT * FROM asset ORDER BY name').fetchall()
    names = _users(conn)
    return [_asset(conn, r, names) for r in rows if ids is None or r['asset_id'] in ids]


def ui_lookups(conn: sqlite3.Connection) -> dict:
    """Options for the dropdowns, from structx's own `input_option` table: the set-up list (modality) and, by catalogue key,
    every select / multi-select of the input catalogue, so the app offers exactly the values structx accepts."""
    lists = repo.option_lists(conn)
    options = {ui_key: lists[sid] for sid, (where, ui_key) in INPUTS.items() if sid in lists and where in ('asset', 'ind', 'comp')
               and ui_key != 'euPrice'}  # euPrice's options are the EU reference countries, which the app already has
    return {'modalities': lists.get('therapeutic_modality', []), 'options': options}
