"""SQLite WAL persistence adapters."""

from .blob_store import BlobRef, ContentAddressedBlobStore
from .capability_repository import (
    CapabilityReceiptRepository,
    SkillPolicyRepository,
    ToolReceiptRepository,
)
from .connection import SqliteDatabase
from .changeset_repository import ChangesetRepository
from .coverage_repository import CoverageRepository, VisualRegionRepository
from .document_repository import (
    DocumentRepository,
    StaleIndexAttempt,
)
from .event_repository import EventRepository
from .legacy_importer import LegacyImportSummary, LegacyMetadataImporter
from .metadata_repository import MetadataRepository
from .migration_backup import (
    MigrationBackup,
    MigrationBackupError,
    MigrationBackupStore,
)
from .run_repository import RunRepository
from .reference_index_repository import ReferenceIndexRepository
from .turn_repository import TurnPage, TurnRepository
from .workspace_repository import WorkspaceRepository

__all__ = [
    "BlobRef",
    "CapabilityReceiptRepository",
    "ChangesetRepository",
    "ContentAddressedBlobStore",
    "CoverageRepository",
    "DocumentRepository",
    "EventRepository",
    "LegacyImportSummary",
    "LegacyMetadataImporter",
    "MetadataRepository",
    "MigrationBackup",
    "MigrationBackupError",
    "MigrationBackupStore",
    "RunRepository",
    "ReferenceIndexRepository",
    "SqliteDatabase",
    "SkillPolicyRepository",
    "StaleIndexAttempt",
    "TurnPage",
    "TurnRepository",
    "ToolReceiptRepository",
    "VisualRegionRepository",
    "WorkspaceRepository",
]
