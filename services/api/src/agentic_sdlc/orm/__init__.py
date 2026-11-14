"""ORM models. Importing this package registers every table on `Base.metadata`."""

from agentic_sdlc.orm.ai import (
    AgentRun,
    EvaluationRun,
    ModelRegistryEntry,
    SkillDefinition,
    SkillVersion,
    TokenUsage,
    ToolInvocation,
)
from agentic_sdlc.orm.approvals import ApprovalDecision, ApprovalRequest
from agentic_sdlc.orm.architecture import ArchitectureDecisionRecord, ArchitectureProposal, ImplementationPlan
from agentic_sdlc.orm.audit import AuditEvent
from agentic_sdlc.orm.backlog import (
    BacklogDependency,
    BacklogItem,
    backlog_item_criteria,
    backlog_item_requirements,
)
from agentic_sdlc.orm.documents import DocumentBlock, DocumentVersion, SourceDocument
from agentic_sdlc.orm.identity import (
    ApiToken,
    DirectoryGroup,
    ExternalIdentity,
    Membership,
    OidcLoginState,
    User,
)
from agentic_sdlc.orm.integrations import ExternalLink
from agentic_sdlc.orm.projects import Project, ProjectRepository
from agentic_sdlc.orm.quality import (
    CodeChange,
    Defect,
    SecurityAcceptance,
    SecurityScan,
    TestRun,
    VerificationResult,
)
from agentic_sdlc.orm.releases import Deployment, Release
from agentic_sdlc.orm.requirements import (
    AcceptanceCriterion,
    ArtifactRevision,
    BusinessRule,
    OpenQuestion,
    Requirement,
)
from agentic_sdlc.orm.workflows import (
    RateLimitWindow,
    WorkerHeartbeat,
    WorkflowEvent,
    WorkflowJob,
    WorkflowRun,
)

__all__ = [
    "AcceptanceCriterion",
    "AgentRun",
    "ApiToken",
    "ApprovalDecision",
    "ApprovalRequest",
    "ArchitectureDecisionRecord",
    "ArchitectureProposal",
    "ArtifactRevision",
    "AuditEvent",
    "BacklogDependency",
    "BacklogItem",
    "BusinessRule",
    "CodeChange",
    "Defect",
    "Deployment",
    "DirectoryGroup",
    "DocumentBlock",
    "DocumentVersion",
    "EvaluationRun",
    "ExternalIdentity",
    "ExternalLink",
    "ImplementationPlan",
    "Membership",
    "ModelRegistryEntry",
    "OidcLoginState",
    "OpenQuestion",
    "Project",
    "ProjectRepository",
    "RateLimitWindow",
    "Release",
    "Requirement",
    "SecurityAcceptance",
    "SecurityScan",
    "SkillDefinition",
    "SkillVersion",
    "SourceDocument",
    "TestRun",
    "TokenUsage",
    "ToolInvocation",
    "User",
    "VerificationResult",
    "WorkerHeartbeat",
    "WorkflowEvent",
    "WorkflowJob",
    "WorkflowRun",
    "backlog_item_criteria",
    "backlog_item_requirements",
]
