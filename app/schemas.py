"""Request bodies, validated at the boundary. Shapes mirror src/types/domain.ts in the React app.

Unknown keys are rejected (extra='forbid') and every string has a maximum length. Nested
workspace data the UI owns (asset.aia, asset.context, decision traces) is accepted as JSON
objects; the request size limit in main.py bounds it.
"""
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

S20 = Annotated[str, StringConstraints(max_length=20)]
S40 = Annotated[str, StringConstraints(max_length=40)]
S60 = Annotated[str, StringConstraints(max_length=60)]
S80 = Annotated[str, StringConstraints(max_length=80)]
S200 = Annotated[str, StringConstraints(max_length=200)]
S300 = Annotated[str, StringConstraints(max_length=300)]
S2000 = Annotated[str, StringConstraints(max_length=2000)]
S4000 = Annotated[str, StringConstraints(max_length=4000)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Email = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=80, pattern=r'^[^\s@]+@[^\s@]+\.[^\s@]+$')]
AssetId = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$')]
RecordId = Annotated[str, StringConstraints(min_length=1, max_length=300)]
Num = int | float

Role = Literal['Admin', 'Editor', 'Viewer']
PolicyCode = Literal['IRA', 'MFN', 'JCA', 'EU-GPL']


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class AssetIn(Strict):
    id: AssetId
    assetCode: S40
    assetName: S200
    legacyAlias: S200 | None = None
    therapeuticArea: S80
    stage: S60
    jurisdictions: Annotated[list[S20], Field(max_length=10)]
    policyExposures: Annotated[list[PolicyCode], Field(max_length=4)]
    submissionStatus: S60
    submissionCompleteness: Annotated[Num, Field(ge=0, le=100)]
    eligibilityStatus: S60
    businessContextStatus: S60
    impactStatus: S60
    owner: S60
    lastUpdatedAt: S40
    estimatedImpactLevel: S40
    estimatedAtRisk: Num
    estimatedOpportunity: Num
    recommendationStatus: S60
    governanceAlerts: Annotated[int, Field(ge=0)]
    summaryMetrics: dict[S40, Num]
    eligibility: dict[PolicyCode, S60]
    context: dict[S40, Any]
    policyImpact: dict[S20, Num]
    recommendationCount: Annotated[int, Field(ge=0)]
    approvalCount: Annotated[int, Field(ge=0)]
    createdByRole: Role | None = None
    workflowStatus: S20 | None = None  # server-owned (see Asset.status); accepted so a GET can be saved back, then ignored
    assessments: Annotated[list[dict[str, Any]], Field(max_length=200)] | None = None
    aia: dict[str, Any] | None = None


class FieldHistoryIn(Strict):
    time: S40
    who: S60
    text: S300


class FieldIn(Strict):
    id: RecordId
    assetId: AssetId
    gen: bool | None = None
    group: S200
    field: S200
    value: S2000
    alternate: S2000 | None = None
    prev: S2000 | None = None
    confidence: S20
    source: S300
    feeds: Annotated[list[S20], Field(max_length=10)]
    status: S40
    history: Annotated[list[FieldHistoryIn], Field(max_length=500)] | None = None
    indId: S40 | None = None
    confirmedBy: S60 | None = None


class QuestionIn(Strict):
    id: RecordId
    assetId: AssetId
    gen: bool | None = None
    group: S200
    text: S2000
    options: Annotated[list[S200], Field(max_length=30)] | None = None
    perIndication: bool | None = None
    ai: S2000
    answer: S200
    answers: dict[S40, S200] | None = None
    evidence: S4000
    source: S300
    confidence: S20
    policies: Annotated[list[PolicyCode], Field(max_length=4)]
    confirmedBy: S60 | None = None


class NewAssetIn(Strict):
    """POST /assets: the asset plus its starter records (assetService.create in the React app)."""
    asset: AssetIn
    fields: Annotated[list[FieldIn], Field(max_length=3000)] = []
    questions: Annotated[list[QuestionIn], Field(max_length=500)] = []
    trace: dict[str, Any] | None = None


class DocumentIn(Strict):
    id: int | None = None  # the browser's temporary id; the server assigns the real one on upload
    assetId: AssetId
    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    size: S40
    type: S60
    types: Annotated[list[S60], Field(max_length=10)] | None = None
    scope: S60
    version: S60
    tag: S60
    slideRef: S60 | None = None
    linkedDocId: int | None = None
    pages: S60
    added: S40
    addedStamp: S40 | None = None
    competitor: bool | None = None
    scanStatus: S20 | None = None  # server-owned; accepted so a GET can be saved back, then ignored


class UserIn(Strict):
    id: Annotated[int, Field(ge=1)]
    name: Name
    email: Email
    role: Role
    roleCode: S40 | None = None
    active: bool
    status: Literal['active', 'inactive', 'pending']
    last: S80
    asset: S200
    assetIds: Annotated[list[AssetId], Field(max_length=500)]
    # Display labels in the mock data ("All assets"); the API returns assetIds here, as the React store does.
    assignedAssets: Annotated[list[S200], Field(max_length=500)] | None = None
    permissions: Annotated[list[S60], Field(max_length=50)] | None = None


class ScenarioIn(Strict):
    name: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    time: S40
    by: S60
    vals: dict[S40, Num | S80]
    delta: Num


class ActivityIn(Strict):
    """The React app also sends user and time; the server ignores them and uses the signed-in user and now."""
    event: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    user: S60 | None = None
    time: S40 | None = None


class CopilotIn(Strict):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    assetId: AssetId | None = None


# ---------------- Data Submission ("Extract data with AI": set-up fields and every file in one request)

DocType = Literal['CI', 'Protocol', 'CDP', 'Forecast']
YesNo = Literal['Yes', 'No']
# Same options as the React Data Submission form (src/engine/constants.ts, src/engine/inputCatalogue.ts).
TherapeuticArea = Literal['Oncology', 'Rare hematology', 'Immunology', 'Cell therapy', 'Neuroscience', 'Metabolic disease', 'Other']
Modality = Literal['Small molecule', 'Biologic (other)', 'Biologic – plasma-derived', 'CGT']


class SubmissionDocIn(Strict):
    docType: DocType


class SubmissionIn(Strict):
    """`metadata` part of POST /assets/submissions. `documents[i]` describes the i-th `files` part."""
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40, pattern=r'^[^\x00-\x1f\x7f]+$')]
    area: TherapeuticArea | None = None
    modality: Modality
    jcaInsights: YesNo | None = None
    regStrategy: YesNo | None = None
    euAccess: YesNo | None = None
    documents: Annotated[list[SubmissionDocIn], Field(min_length=1, max_length=100)]
