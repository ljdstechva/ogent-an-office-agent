"""Validated, non-secret Ogent feature flags and resource quotas."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping


MIB = 1024 * 1024
GIB = 1024 * MIB


class SettingsError(ValueError):
    """A public startup error for an invalid Ogent configuration."""


def _integer(
    environ: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = str(environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise SettingsError(f"{name} must be between {minimum:,} and {maximum:,}.")
    return value


def _boolean(
    environ: Mapping[str, str],
    name: str,
    default: bool,
) -> bool:
    raw = str(environ.get(name, "")).strip().casefold()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise SettingsError(f"{name} must be true or false.")


@dataclasses.dataclass(frozen=True, slots=True)
class FeatureFlags:
    large_text_assets: bool = True
    warm_provider_transport: bool = False
    strict_disk_forecast: bool = True
    fault_injection: bool = False

    @classmethod
    def load(cls, environ: Mapping[str, str]) -> "FeatureFlags":
        legacy_warm = _boolean(
            environ,
            "OGENT_ENABLE_WARM_PROVIDER_TRANSPORT",
            False,
        )
        return cls(
            large_text_assets=_boolean(
                environ,
                "OGENT_FEATURE_LARGE_TEXT_ASSETS",
                True,
            ),
            warm_provider_transport=_boolean(
                environ,
                "OGENT_FEATURE_WARM_PROVIDER_TRANSPORT",
                legacy_warm,
            ),
            strict_disk_forecast=_boolean(
                environ,
                "OGENT_FEATURE_STRICT_DISK_FORECAST",
                True,
            ),
            fault_injection=_boolean(
                environ,
                "OGENT_ENABLE_FAULT_INJECTION",
                False,
            ),
        )

    def public(self) -> dict[str, bool]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True, slots=True)
class ResourceQuotas:
    max_proxy_body_bytes: int = 64 * 1024
    max_json_body_bytes: int = MIB
    max_inline_turn_characters: int = 200_000
    max_document_upload_bytes: int = 128 * MIB
    max_reference_file_bytes: int = 50 * MIB
    max_references_per_turn: int = 20
    max_reference_turn_bytes: int = 100 * MIB
    max_session_reference_count: int = 100
    max_session_reference_bytes: int = 500 * MIB
    max_concurrent_reference_uploads: int = 3
    max_local_data_bytes: int = 8 * GIB
    minimum_free_disk_bytes: int = 512 * MIB
    partial_retention_seconds: int = 24 * 60 * 60
    max_log_bytes: int = 10 * MIB
    log_backup_count: int = 3

    @classmethod
    def load(cls, environ: Mapping[str, str]) -> "ResourceQuotas":
        values = cls(
            max_proxy_body_bytes=_integer(
                environ,
                "OGENT_MAX_PROXY_BODY_BYTES",
                64 * 1024,
                minimum=16 * 1024,
                maximum=8 * MIB,
            ),
            max_json_body_bytes=_integer(
                environ,
                "OGENT_MAX_JSON_BODY_BYTES",
                MIB,
                minimum=MIB,
                maximum=16 * MIB,
            ),
            max_inline_turn_characters=_integer(
                environ,
                "OGENT_MAX_INLINE_TURN_CHARACTERS",
                200_000,
                minimum=10_000,
                maximum=1_000_000,
            ),
            max_document_upload_bytes=_integer(
                environ,
                "OGENT_MAX_DOCUMENT_UPLOAD_BYTES",
                128 * MIB,
                minimum=MIB,
                maximum=2 * GIB,
            ),
            max_reference_file_bytes=_integer(
                environ,
                "OGENT_MAX_REFERENCE_FILE_BYTES",
                50 * MIB,
                minimum=MIB,
                maximum=2 * GIB,
            ),
            max_references_per_turn=_integer(
                environ,
                "OGENT_MAX_REFERENCES_PER_TURN",
                20,
                minimum=1,
                maximum=100,
            ),
            max_reference_turn_bytes=_integer(
                environ,
                "OGENT_MAX_REFERENCE_TURN_BYTES",
                100 * MIB,
                minimum=MIB,
                maximum=4 * GIB,
            ),
            max_session_reference_count=_integer(
                environ,
                "OGENT_MAX_SESSION_REFERENCE_COUNT",
                100,
                minimum=1,
                maximum=1_000,
            ),
            max_session_reference_bytes=_integer(
                environ,
                "OGENT_MAX_SESSION_REFERENCE_BYTES",
                500 * MIB,
                minimum=MIB,
                maximum=16 * GIB,
            ),
            max_concurrent_reference_uploads=_integer(
                environ,
                "OGENT_MAX_CONCURRENT_REFERENCE_UPLOADS",
                3,
                minimum=1,
                maximum=16,
            ),
            max_local_data_bytes=_integer(
                environ,
                "OGENT_MAX_LOCAL_DATA_BYTES",
                8 * GIB,
                minimum=256 * MIB,
                maximum=256 * GIB,
            ),
            minimum_free_disk_bytes=_integer(
                environ,
                "OGENT_MINIMUM_FREE_DISK_BYTES",
                512 * MIB,
                minimum=64 * MIB,
                maximum=64 * GIB,
            ),
            partial_retention_seconds=_integer(
                environ,
                "OGENT_PARTIAL_RETENTION_SECONDS",
                24 * 60 * 60,
                minimum=60,
                maximum=30 * 24 * 60 * 60,
            ),
            max_log_bytes=_integer(
                environ,
                "OGENT_MAX_LOG_BYTES",
                10 * MIB,
                minimum=256 * 1024,
                maximum=GIB,
            ),
            log_backup_count=_integer(
                environ,
                "OGENT_LOG_BACKUP_COUNT",
                3,
                minimum=1,
                maximum=20,
            ),
        )
        values.validate()
        return values

    def validate(self) -> None:
        if self.max_reference_turn_bytes < self.max_reference_file_bytes:
            raise SettingsError(
                "OGENT_MAX_REFERENCE_TURN_BYTES must be at least the "
                "per-file reference limit."
            )
        if self.max_session_reference_bytes < self.max_reference_turn_bytes:
            raise SettingsError(
                "OGENT_MAX_SESSION_REFERENCE_BYTES must be at least the "
                "per-turn reference limit."
            )
        if self.max_session_reference_count < self.max_references_per_turn:
            raise SettingsError(
                "OGENT_MAX_SESSION_REFERENCE_COUNT must be at least the "
                "per-turn reference count."
            )
        worst_case_inline_bytes = self.max_inline_turn_characters * 4 + 4096
        if self.max_json_body_bytes < worst_case_inline_bytes:
            raise SettingsError(
                "OGENT_MAX_JSON_BODY_BYTES is too small for the configured "
                "inline turn limit."
            )
        if self.max_local_data_bytes <= self.minimum_free_disk_bytes:
            raise SettingsError(
                "OGENT_MAX_LOCAL_DATA_BYTES must exceed the free-disk reserve."
            )

    def public(self) -> dict[str, int]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True, slots=True)
class OgentSettings:
    features: FeatureFlags
    quotas: ResourceQuotas

    @classmethod
    def load(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "OgentSettings":
        source = os.environ if environ is None else environ
        return cls(
            features=FeatureFlags.load(source),
            quotas=ResourceQuotas.load(source),
        )


DEFAULT_SETTINGS = OgentSettings.load()
