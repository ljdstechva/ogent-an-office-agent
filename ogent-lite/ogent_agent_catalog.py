"""Dynamic agent capability catalogs and cache management for Ogent.

The installed command-line tools are the only source of model and effort
choices.  This module normalizes provider results, persists secret-free cached
catalogs, coordinates bounded background refreshes, and validates browser
selections against a successful live refresh from the current process.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Protocol


SCHEMA_VERSION = 1
CACHE_FRESHNESS_SECONDS = 6 * 60 * 60
AUTOMATIC_EFFORT = "automatic"


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def normalize_executable_path(path: str) -> str:
    return os.path.normcase(str(Path(path).resolve(strict=False)))


def _clean_string(value: Any, *, maximum: int = 512) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or "\x00" in text:
        raise ValueError("Catalog text is empty or invalid.")
    return text


def _clean_optional_string(value: Any, *, maximum: int = 512) -> str | None:
    if value is None:
        return None
    return _clean_string(value, maximum=maximum)


def _clean_string_tuple(
    value: Any,
    *,
    maximum_items: int = 128,
    maximum_length: int = 256,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("Catalog list is invalid.")
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        item = _clean_string(raw, maximum=maximum_length)
        if item in seen:
            continue
        result.append(item)
        seen.add(item)
        if len(result) > maximum_items:
            raise ValueError("Catalog list is too large.")
    return tuple(result)


@dataclasses.dataclass(frozen=True)
class ModelCapability:
    id: str
    display_name: str
    efforts: tuple[str, ...]
    default_effort: str | None
    input_modalities: tuple[str, ...]
    is_default: bool
    capability_source: str
    efforts_verified: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _clean_string(self.id, maximum=256))
        object.__setattr__(
            self,
            "display_name",
            _clean_string(self.display_name, maximum=256),
        )
        object.__setattr__(
            self,
            "efforts",
            _clean_string_tuple(self.efforts, maximum_items=64),
        )
        object.__setattr__(
            self,
            "default_effort",
            _clean_optional_string(self.default_effort, maximum=128),
        )
        object.__setattr__(
            self,
            "input_modalities",
            _clean_string_tuple(self.input_modalities, maximum_items=32),
        )
        object.__setattr__(
            self,
            "capability_source",
            _clean_string(self.capability_source, maximum=128),
        )
        if (
            self.default_effort is not None
            and self.default_effort not in self.efforts
        ):
            object.__setattr__(self, "default_effort", None)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "displayName": self.display_name,
            "efforts": list(self.efforts),
            "defaultEffort": self.default_effort,
            "inputModalities": list(self.input_modalities),
            "isDefault": self.is_default,
            "capabilitySource": self.capability_source,
            "effortsVerified": self.efforts_verified,
        }

    def cache_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "displayName": self.display_name,
            "efforts": list(self.efforts),
            "defaultEffort": self.default_effort,
            "inputModalities": list(self.input_modalities),
            "isDefault": self.is_default,
            "capabilitySource": self.capability_source,
            "effortsVerified": self.efforts_verified,
        }

    @classmethod
    def from_cache_dict(cls, value: Any) -> "ModelCapability":
        if not isinstance(value, dict):
            raise ValueError("Cached model entry is invalid.")
        return cls(
            id=value.get("id"),
            display_name=value.get("displayName"),
            efforts=_clean_string_tuple(value.get("efforts", [])),
            default_effort=value.get("defaultEffort"),
            input_modalities=_clean_string_tuple(
                value.get("inputModalities", [])
            ),
            is_default=bool(value.get("isDefault")),
            capability_source=value.get("capabilitySource") or "cli",
            efforts_verified=bool(value.get("effortsVerified")),
        )


@dataclasses.dataclass(frozen=True)
class ProviderCatalog:
    provider_id: str
    label: str
    installed: bool
    authenticated: bool
    cli_path: str | None
    cli_version: str | None
    status: str
    models: tuple[ModelCapability, ...]
    refreshed_at: str | None
    stale: bool
    warning: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_id",
            _clean_string(self.provider_id, maximum=64),
        )
        object.__setattr__(self, "label", _clean_string(self.label, maximum=128))
        object.__setattr__(self, "status", _clean_string(self.status, maximum=64))
        object.__setattr__(
            self,
            "cli_path",
            _clean_optional_string(self.cli_path, maximum=2048),
        )
        object.__setattr__(
            self,
            "cli_version",
            _clean_optional_string(self.cli_version, maximum=256),
        )
        object.__setattr__(
            self,
            "refreshed_at",
            _clean_optional_string(self.refreshed_at, maximum=128),
        )
        object.__setattr__(
            self,
            "warning",
            _clean_optional_string(self.warning, maximum=1000),
        )
        if not isinstance(self.models, tuple):
            object.__setattr__(self, "models", tuple(self.models))
        model_ids: set[str] = set()
        for model in self.models:
            if not isinstance(model, ModelCapability):
                raise ValueError("Provider catalog contains an invalid model.")
            if model.id in model_ids:
                raise ValueError("Provider catalog contains a duplicate model.")
            model_ids.add(model.id)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "label": self.label,
            "installed": self.installed,
            "authenticated": self.authenticated,
            "status": self.status,
            "cliVersion": self.cli_version,
            "live": self.status == "ready" and not self.stale,
            "stale": self.stale,
            "models": [model.public_dict() for model in self.models],
            "refreshedAt": self.refreshed_at,
            "warning": self.warning,
        }

    def cache_dict(self) -> dict[str, Any]:
        return {
            "providerId": self.provider_id,
            "label": self.label,
            "refreshedAt": self.refreshed_at,
            "models": [model.cache_dict() for model in self.models],
        }

    @classmethod
    def from_cache_dict(
        cls,
        value: Any,
        *,
        cli_path: str,
        cli_version: str,
    ) -> "ProviderCatalog":
        if not isinstance(value, dict):
            raise ValueError("Cached provider catalog is invalid.")
        raw_models = value.get("models")
        if not isinstance(raw_models, list):
            raise ValueError("Cached provider models are invalid.")
        return cls(
            provider_id=value.get("providerId"),
            label=value.get("label"),
            installed=True,
            authenticated=False,
            cli_path=cli_path,
            cli_version=cli_version,
            status="cached",
            models=tuple(
                ModelCapability.from_cache_dict(model) for model in raw_models
            ),
            refreshed_at=value.get("refreshedAt"),
            stale=True,
            warning="Using cached information while refreshing.",
        )


@dataclasses.dataclass(frozen=True)
class ProviderEnvironment:
    provider_id: str
    label: str
    installed: bool
    authenticated: bool
    cli_path: str | None
    cli_version: str | None
    command: tuple[str, ...]
    status: str
    warning: str | None = None


@dataclasses.dataclass(frozen=True)
class EffortVerificationResult:
    efforts: tuple[str, ...]
    default_effort: str | None = None
    warning: str | None = None
    use_global_unverified: bool = False
    inference_detected: bool = False


@dataclasses.dataclass(frozen=True)
class AgentSelection:
    provider_id: str
    model: str
    effort: str

    @property
    def model_id(self) -> str:
        return self.model


class CatalogDiscoveryError(RuntimeError):
    def __init__(self, message: str, *, status: str = "catalog_error") -> None:
        super().__init__(message)
        self.status = status


class SelectionValidationError(ValueError):
    pass


class AgentCatalogError(RuntimeError):
    """Compatibility error carrying a stable browser-facing category."""

    def __init__(self, message: str, *, code: str = "catalog_error") -> None:
        super().__init__(message)
        self.code = code


class AgentProviderProtocol(Protocol):
    provider_id: str
    label: str
    supports_model_effort_verification: bool

    def inspect_environment(self) -> ProviderEnvironment:
        ...

    def discover_catalog(
        self,
        environment: ProviderEnvironment,
    ) -> ProviderCatalog:
        ...

    def verify_model_efforts(
        self,
        environment: ProviderEnvironment,
        model_id: str,
    ) -> EffortVerificationResult:
        ...

    def cancel_discovery(self) -> None:
        ...


class CapabilityCache:
    """Versioned, atomic, secret-free provider capability cache."""

    def __init__(
        self,
        path: Path,
        *,
        freshness_seconds: float = CACHE_FRESHNESS_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.freshness_seconds = freshness_seconds
        self.clock = clock
        self.lock = threading.RLock()

    @staticmethod
    def _entry_key(
        provider_id: str,
        cli_path: str,
        cli_version: str,
    ) -> str:
        normalized = normalize_executable_path(cli_path)
        payload = json.dumps(
            [provider_id, normalized, cli_version],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schemaVersion": SCHEMA_VERSION, "entries": {}}
        if (
            not isinstance(value, dict)
            or value.get("schemaVersion") != SCHEMA_VERSION
            or not isinstance(value.get("entries"), dict)
        ):
            return {"schemaVersion": SCHEMA_VERSION, "entries": {}}
        return value

    def load(
        self,
        provider_id: str,
        cli_path: str,
        cli_version: str,
    ) -> ProviderCatalog | None:
        normalized = normalize_executable_path(cli_path)
        key = self._entry_key(provider_id, normalized, cli_version)
        with self.lock:
            payload = self._read_unlocked()
        entry = payload["entries"].get(key)
        if not isinstance(entry, dict):
            return None
        if (
            entry.get("providerId") != provider_id
            or entry.get("cliPath") != normalized
            or entry.get("cliVersion") != cli_version
        ):
            return None
        try:
            saved_at = float(entry.get("savedAt"))
        except (TypeError, ValueError):
            return None
        if saved_at > self.clock() + 300:
            return None
        if self.clock() - saved_at > self.freshness_seconds:
            return None
        try:
            return ProviderCatalog.from_cache_dict(
                entry.get("catalog"),
                cli_path=normalized,
                cli_version=cli_version,
            )
        except ValueError:
            return None

    def store(self, catalog: ProviderCatalog) -> None:
        if (
            catalog.status != "ready"
            or catalog.stale
            or not catalog.installed
            or not catalog.authenticated
            or not catalog.cli_path
            or not catalog.cli_version
        ):
            return
        normalized = normalize_executable_path(catalog.cli_path)
        key = self._entry_key(
            catalog.provider_id,
            normalized,
            catalog.cli_version,
        )
        with self.lock:
            payload = self._read_unlocked()
            entries = payload["entries"]
            for existing_key, entry in list(entries.items()):
                if (
                    isinstance(entry, dict)
                    and entry.get("providerId") == catalog.provider_id
                    and existing_key != key
                ):
                    entries.pop(existing_key, None)
            entries[key] = {
                "providerId": catalog.provider_id,
                "cliPath": normalized,
                "cliVersion": catalog.cli_version,
                "savedAt": self.clock(),
                "catalog": catalog.cache_dict(),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(
                f".{self.path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                    json.dump(
                        payload,
                        stream,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass


class CapabilityManager:
    """Coordinates live provider refresh, lazy effort probes, and validation."""

    def __init__(
        self,
        providers: tuple[AgentProviderProtocol, ...],
        cache: CapabilityCache,
        *,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.providers = {provider.provider_id: provider for provider in providers}
        if len(self.providers) != len(providers):
            raise ValueError("Provider identifiers must be unique.")
        self.cache = cache
        self.on_change = on_change
        self.lock = threading.RLock()
        self.catalogs: dict[str, ProviderCatalog] = {
            provider.provider_id: ProviderCatalog(
                provider_id=provider.provider_id,
                label=provider.label,
                installed=False,
                authenticated=False,
                cli_path=None,
                cli_version=None,
                status="checking",
                models=(),
                refreshed_at=None,
                stale=True,
                warning=None,
            )
            for provider in providers
        }
        self.environments: dict[str, ProviderEnvironment] = {}
        self.refreshing_providers: set[str] = set()
        self.probing_models: set[tuple[str, str]] = set()
        self.completed_model_probes: set[tuple[str, str]] = set()
        self._shutdown = False

    def _notify(self) -> None:
        callback = self.on_change
        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            catalogs = [
                self.catalogs[provider_id]
                for provider_id in self.providers
            ]
            refreshing = sorted(self.refreshing_providers)
            probing_keys = set(self.probing_models)
            probing = [
                {"provider": provider_id, "model": model_id}
                for provider_id, model_id in sorted(probing_keys)
        ]
        public_providers: list[dict[str, Any]] = []
        for catalog in catalogs:
            public = catalog.public_dict()
            public["live"] = catalog.status == "ready" and not catalog.stale
            public_providers.append(public)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "refreshing": bool(refreshing),
            "refreshingProviders": refreshing,
            "probing": probing,
            "providers": public_providers,
        }

    def get_catalog(self, provider_id: str) -> ProviderCatalog | None:
        with self.lock:
            return self.catalogs.get(provider_id)

    def set_catalog_for_testing(self, catalog: ProviderCatalog) -> None:
        if catalog.provider_id not in self.providers:
            raise ValueError("Unknown provider.")
        with self.lock:
            self.catalogs[catalog.provider_id] = catalog

    def refresh_async(self, provider_id: str | None = None) -> bool:
        if provider_id is None:
            provider_ids = tuple(self.providers)
        else:
            if provider_id not in self.providers:
                raise SelectionValidationError("Unknown AI provider.")
            provider_ids = (provider_id,)
        started = False
        for current_id in provider_ids:
            with self.lock:
                if self._shutdown or current_id in self.refreshing_providers:
                    continue
                self.refreshing_providers.add(current_id)
                self.completed_model_probes = {
                    key
                    for key in self.completed_model_probes
                    if key[0] != current_id
                }
                existing = self.catalogs[current_id]
                self.catalogs[current_id] = dataclasses.replace(
                    existing,
                    status="refreshing",
                    stale=True,
                    warning=(
                        "Using cached information while refreshing."
                        if existing.models
                        else existing.warning
                    ),
                )
            thread = threading.Thread(
                target=self._refresh_worker,
                args=(current_id,),
                name=f"ogent-catalog-{current_id}",
                daemon=True,
            )
            thread.start()
            started = True
        if started:
            self._notify()
        return started

    def refresh_now(self, provider_id: str) -> ProviderCatalog:
        if provider_id not in self.providers:
            raise SelectionValidationError("Unknown AI provider.")
        with self.lock:
            if self._shutdown:
                raise RuntimeError("Capability manager is shutting down.")
            if provider_id in self.refreshing_providers:
                raise RuntimeError("Provider refresh is already running.")
            self.refreshing_providers.add(provider_id)
        self._refresh_worker(provider_id)
        catalog = self.get_catalog(provider_id)
        assert catalog is not None
        return catalog

    def _publish_cached(
        self,
        environment: ProviderEnvironment,
    ) -> ProviderCatalog | None:
        if not environment.cli_path or not environment.cli_version:
            return None
        cached = self.cache.load(
            environment.provider_id,
            environment.cli_path,
            environment.cli_version,
        )
        if cached is None:
            return None
        status = "refreshing"
        warning = "Using cached information while refreshing."
        if not environment.authenticated:
            status = "auth_required"
            warning = environment.warning
        published = dataclasses.replace(
            cached,
            installed=environment.installed,
            authenticated=environment.authenticated,
            cli_path=environment.cli_path,
            cli_version=environment.cli_version,
            status=status,
            stale=True,
            warning=warning,
        )
        with self.lock:
            self.catalogs[environment.provider_id] = published
        self._notify()
        return published

    def _refresh_worker(self, provider_id: str) -> None:
        provider = self.providers[provider_id]
        cached: ProviderCatalog | None = None
        try:
            environment = provider.inspect_environment()
            with self.lock:
                if self._shutdown:
                    return
                self.environments[provider_id] = environment
            cached = self._publish_cached(environment)
            if not environment.installed:
                catalog = ProviderCatalog(
                    provider_id=provider_id,
                    label=provider.label,
                    installed=False,
                    authenticated=False,
                    cli_path=None,
                    cli_version=None,
                    status="not_installed",
                    models=(),
                    refreshed_at=None,
                    stale=False,
                    warning=environment.warning,
                )
            elif environment.status == "catalog_error":
                catalog = ProviderCatalog(
                    provider_id=provider_id,
                    label=provider.label,
                    installed=True,
                    authenticated=False,
                    cli_path=environment.cli_path,
                    cli_version=environment.cli_version,
                    status="catalog_error",
                    models=cached.models if cached else (),
                    refreshed_at=cached.refreshed_at if cached else None,
                    stale=bool(cached),
                    warning=environment.warning,
                )
            elif not environment.authenticated:
                catalog = ProviderCatalog(
                    provider_id=provider_id,
                    label=provider.label,
                    installed=True,
                    authenticated=False,
                    cli_path=environment.cli_path,
                    cli_version=environment.cli_version,
                    status="auth_required",
                    models=cached.models if cached else (),
                    refreshed_at=cached.refreshed_at if cached else None,
                    stale=bool(cached),
                    warning=environment.warning,
                )
            else:
                catalog = provider.discover_catalog(environment)
                if catalog.provider_id != provider_id:
                    raise CatalogDiscoveryError(
                        "The provider returned a mismatched catalog."
                    )
                catalog = dataclasses.replace(
                    catalog,
                    installed=True,
                    authenticated=True,
                    cli_path=environment.cli_path,
                    cli_version=environment.cli_version,
                    status="ready",
                    stale=False,
                    refreshed_at=utc_now_iso(),
                )
                self.cache.store(catalog)
            with self.lock:
                if not self._shutdown:
                    self.catalogs[provider_id] = catalog
        except CatalogDiscoveryError as exc:
            if cached is not None:
                catalog = dataclasses.replace(
                    cached,
                    status=exc.status,
                    stale=True,
                    warning=str(exc),
                )
            else:
                environment = self.environments.get(provider_id)
                catalog = ProviderCatalog(
                    provider_id=provider_id,
                    label=provider.label,
                    installed=bool(environment and environment.installed),
                    authenticated=bool(
                        environment and environment.authenticated
                    ),
                    cli_path=environment.cli_path if environment else None,
                    cli_version=(
                        environment.cli_version if environment else None
                    ),
                    status=exc.status,
                    models=(),
                    refreshed_at=None,
                    stale=False,
                    warning=str(exc),
                )
            with self.lock:
                if not self._shutdown:
                    self.catalogs[provider_id] = catalog
        except Exception as exc:
            warning = (
                "This CLI version does not expose a compatible catalog. "
                f"{str(exc).strip()}"
            ).strip()
            if cached is not None:
                catalog = dataclasses.replace(
                    cached,
                    status="catalog_error",
                    stale=True,
                    warning=warning,
                )
            else:
                environment = self.environments.get(provider_id)
                catalog = ProviderCatalog(
                    provider_id=provider_id,
                    label=provider.label,
                    installed=bool(environment and environment.installed),
                    authenticated=bool(
                        environment and environment.authenticated
                    ),
                    cli_path=environment.cli_path if environment else None,
                    cli_version=(
                        environment.cli_version if environment else None
                    ),
                    status="catalog_error",
                    models=(),
                    refreshed_at=None,
                    stale=False,
                    warning=warning,
                )
            with self.lock:
                if not self._shutdown:
                    self.catalogs[provider_id] = catalog
        finally:
            with self.lock:
                self.refreshing_providers.discard(provider_id)
            self._notify()

    def ensure_model_efforts_async(
        self,
        provider_id: str,
        model_id: str,
    ) -> bool:
        provider = self.providers.get(provider_id)
        if provider is None:
            raise SelectionValidationError("Unknown AI provider.")
        if not provider.supports_model_effort_verification:
            return False
        key = (provider_id, model_id)
        with self.lock:
            catalog = self.catalogs[provider_id]
            if (
                self._shutdown
                or catalog.status != "ready"
                or catalog.stale
                or model_id not in {model.id for model in catalog.models}
                or key in self.probing_models
                or key in self.completed_model_probes
            ):
                return False
            model = next(item for item in catalog.models if item.id == model_id)
            if model.efforts:
                return False
            environment = self.environments.get(provider_id)
            if environment is None:
                return False
            self.probing_models.add(key)
        thread = threading.Thread(
            target=self._probe_worker,
            args=(provider, environment, model_id),
            name=f"ogent-efforts-{provider_id}",
            daemon=True,
        )
        thread.start()
        self._notify()
        return True

    def _probe_worker(
        self,
        provider: AgentProviderProtocol,
        environment: ProviderEnvironment,
        model_id: str,
    ) -> None:
        key = (provider.provider_id, model_id)
        try:
            result = provider.verify_model_efforts(environment, model_id)
            with self.lock:
                if self._shutdown:
                    return
                current_environment = self.environments.get(provider.provider_id)
                catalog = self.catalogs[provider.provider_id]
                if (
                    current_environment is None
                    or current_environment.cli_path != environment.cli_path
                    or current_environment.cli_version
                    != environment.cli_version
                    or catalog.status != "ready"
                    or catalog.stale
                ):
                    return
                models: list[ModelCapability] = []
                found = False
                for model in catalog.models:
                    if model.id != model_id:
                        models.append(model)
                        continue
                    found = True
                    default_effort = result.default_effort
                    if default_effort not in result.efforts:
                        default_effort = None
                    models.append(
                        dataclasses.replace(
                            model,
                            efforts=tuple(result.efforts),
                            default_effort=default_effort,
                            efforts_verified=not result.use_global_unverified,
                        )
                    )
                if not found:
                    return
                warning = result.warning
                if not result.efforts and not warning:
                    warning = (
                        "No model-specific effort control; using CLI default."
                    )
                updated = dataclasses.replace(
                    catalog,
                    models=tuple(models),
                    warning=warning,
                    refreshed_at=utc_now_iso(),
                )
                self.catalogs[provider.provider_id] = updated
            self.cache.store(updated)
        except Exception as exc:
            with self.lock:
                catalog = self.catalogs.get(provider.provider_id)
                if catalog is not None and catalog.status == "ready":
                    self.catalogs[provider.provider_id] = dataclasses.replace(
                        catalog,
                        warning=(
                            "No model-specific effort control; using CLI "
                            f"default. {str(exc).strip()}"
                        ).strip(),
                    )
        finally:
            with self.lock:
                self.probing_models.discard(key)
                self.completed_model_probes.add(key)
            self._notify()

    def validate_selection(
        self,
        provider_id: Any,
        model_id: Any,
        effort: Any,
    ) -> AgentSelection:
        selected_provider = str(provider_id or "").strip()
        selected_model = str(model_id or "").strip()
        selected_effort = str(effort or "").strip()
        with self.lock:
            catalog = self.catalogs.get(selected_provider)
        if catalog is None:
            raise SelectionValidationError("Unknown AI provider.")
        if not catalog.installed:
            if selected_provider == "codex":
                raise SelectionValidationError("Codex CLI is not installed.")
            if selected_provider == "claude":
                raise SelectionValidationError("Claude Code is not installed.")
            raise SelectionValidationError("The selected provider is not installed.")
        if not catalog.authenticated:
            if selected_provider == "codex":
                raise SelectionValidationError(
                    "Sign in with `codex login`, then refresh."
                )
            if selected_provider == "claude":
                raise SelectionValidationError(
                    "Sign in with `claude auth login`, then refresh."
                )
            raise SelectionValidationError(
                "Sign in through the selected CLI, then refresh."
            )
        if catalog.status != "ready" or catalog.stale:
            raise SelectionValidationError(
                "This provider does not have a usable live model catalog. "
                "Refresh and try again."
            )
        model = next(
            (item for item in catalog.models if item.id == selected_model),
            None,
        )
        if model is None:
            raise SelectionValidationError(
                "The selected model is no longer reported by this CLI. "
                "Refresh and choose again."
            )
        if selected_effort != AUTOMATIC_EFFORT and selected_effort not in model.efforts:
            raise SelectionValidationError(
                "The selected effort is not available for this model. "
                "Refresh and choose again."
            )
        return AgentSelection(
            provider_id=selected_provider,
            model=selected_model,
            effort=selected_effort,
        )

    def shutdown(self) -> None:
        with self.lock:
            self._shutdown = True
            providers = tuple(self.providers.values())
        for provider in providers:
            try:
                provider.cancel_discovery()
            except Exception:
                pass


def provider_label(provider_id: str) -> str:
    labels = {
        "codex": "Codex",
        "claude": "Claude Code",
    }
    return labels.get(provider_id, provider_id)


def resolve_codex_command() -> list[str]:
    """Resolve the shell-free Codex launch array for legacy Ogent callers."""

    from ogent_agent_providers import resolve_codex_cli

    resolution = resolve_codex_cli()
    if resolution is None:
        raise AgentCatalogError(
            "Codex CLI is not installed.",
            code="not_installed",
        )
    return list(resolution.command)


class AgentCatalogManager:
    """Compatibility facade used by the Ogent application entry point."""

    def __init__(self, cache_path: Path) -> None:
        from ogent_agent_providers import build_default_providers

        self._manager = CapabilityManager(
            build_default_providers(),
            CapabilityCache(cache_path),
        )

    @property
    def providers(self) -> dict[str, AgentProviderProtocol]:
        return self._manager.providers

    def public_snapshot(self) -> dict[str, Any]:
        snapshot = self._manager.snapshot()
        for provider in snapshot["providers"]:
            provider["live"] = (
                provider["status"] == "ready" and not provider["stale"]
            )
        return snapshot

    @staticmethod
    def _selection_error_code(message: str) -> str:
        lowered = message.casefold()
        if "unknown ai provider" in lowered:
            return "unknown_provider"
        if "not installed" in lowered:
            return "not_installed"
        if "sign in" in lowered:
            return "auth_required"
        if "model is no longer" in lowered:
            return "unsupported_model"
        if "selected effort" in lowered:
            return "unsupported_effort"
        if "usable live model catalog" in lowered:
            return "not_checked"
        return "catalog_error"

    def validate_selection(
        self,
        provider_id: Any,
        model_id: Any,
        effort: Any,
    ) -> AgentSelection:
        try:
            return self._manager.validate_selection(
                provider_id,
                model_id,
                effort,
            )
        except SelectionValidationError as exc:
            raise AgentCatalogError(
                str(exc),
                code=self._selection_error_code(str(exc)),
            ) from exc

    def refresh_async(self, provider_id: str | None = None) -> bool:
        try:
            return self._manager.refresh_async(provider_id)
        except SelectionValidationError as exc:
            raise AgentCatalogError(
                str(exc),
                code="unknown_provider",
            ) from exc

    def verify_efforts(self, provider_id: str, model_id: str) -> bool:
        if provider_id != "claude":
            raise AgentCatalogError(
                "Model-specific effort verification is unavailable for this provider.",
                code="unsupported_provider",
            )
        catalog = self._manager.get_catalog(provider_id)
        if catalog is None or catalog.status != "ready" or catalog.stale:
            raise AgentCatalogError(
                "Claude Code does not have a usable live model catalog.",
                code="not_checked",
            )
        if model_id not in {model.id for model in catalog.models}:
            raise AgentCatalogError(
                "The selected model is no longer reported by this CLI. "
                "Refresh and choose again.",
                code="unsupported_model",
            )
        return self._manager.ensure_model_efforts_async(provider_id, model_id)

    def shutdown(self) -> None:
        self._manager.shutdown()
