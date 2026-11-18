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
Modality = Literal['Small molecule', 'Biologic (other)', 'Biologic – plasma-derived', 'CGT']


