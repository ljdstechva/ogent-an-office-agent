#!/usr/bin/env python3
"""Per-document OfficeCLI preview revision correlation and client control."""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import hashlib
import json
import re
import secrets
import threading
import time
from typing import Any


DOCUMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,160}$")
GENERATION_PATTERN = re.compile(r"^[0-9a-f]{32}$")
CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
CHANNEL_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,160}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CONTROL_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MUTATION_ACTIONS = frozenset(
    {
        "add",
        "doc-switched",
        "excel-patch",
        "full",
        "remove",
        "replace",
        "word-patch",
    }
)


class PreviewSyncError(RuntimeError):
    """A safe preview synchronization error."""


def utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def event_fingerprint(payload: dict[str, Any]) -> str:
    """Hash the exact semantic watch event without relay-only metadata."""
    clean = {
        str(key): value
        for key, value in payload.items()
        if str(key) != "_ogent"
    }
    encoded = json.dumps(
        clean,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclasses.dataclass(frozen=True)
class PreviewRunBaseline:
    document_id: str
    watch_generation: str
    mutation_sequence: int
    package_sha256: str
    client_id: str | None


@dataclasses.dataclass(frozen=True)
class WatchMutation:
    sequence: int
    document_id: str
    watch_generation: str
    action: str
    version: int
    base_version: int | None
    event_fingerprint: str
    package_sha256: str
    observed_at: str

    def public_metadata(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class PreviewAck:
    kind: str
    client_id: str
    document_id: str
    watch_generation: str
    version: int
    event_fingerprint: str | None
    package_sha256: str
    dom_fingerprint: str | None
    control_id: str | None
    viewport_path: str | None
    acknowledged_at: str
    canonical_dom_fingerprint: str | None = None


@dataclasses.dataclass(frozen=True)
class PreviewConfirmation:
    confirmed: bool
    status: str
    message: str
    mutation: WatchMutation | None = None
    ack: PreviewAck | None = None
    recovery: str | None = None

    def public_metadata(self) -> dict[str, Any]:
        return {
            "confirmed": self.confirmed,
            "status": self.status,
            "message": self.message,
            "watch_version": (
                self.mutation.version
                if self.mutation is not None
                else self.ack.version
                if self.ack is not None
                else None
            ),
            "event_fingerprint": (
                self.mutation.event_fingerprint
                if self.mutation is not None
                else self.ack.event_fingerprint
                if self.ack is not None
                else None
            ),
            "package_sha256": (
                self.mutation.package_sha256
                if self.mutation is not None
                else self.ack.package_sha256
                if self.ack is not None
                else None
            ),
            "client_id": self.ack.client_id if self.ack is not None else None,
            "recovery": self.recovery,
        }


class PreviewSyncState:
    """Correlate watch mutations, rendered-client acknowledgments, and recovery."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.document_id = ""
        self.watch_generation = ""
        self.mutation_sequence = 0
        self.mutations: collections.deque[WatchMutation] = collections.deque(
            maxlen=256
        )
        self.acks: collections.deque[PreviewAck] = collections.deque(maxlen=512)
        self.channels: dict[tuple[str, str, str], str] = {}
        self.control_sequence = 0
        self.controls: dict[
            tuple[str, str, str],
            collections.deque[dict[str, Any]],
        ] = {}

    def activate_watch(self, document_id: str, watch_generation: str) -> None:
        if not DOCUMENT_ID_PATTERN.fullmatch(document_id):
            raise PreviewSyncError("Invalid preview document identity.")
        if not GENERATION_PATTERN.fullmatch(watch_generation):
            raise PreviewSyncError("Invalid preview watch generation.")
        with self.condition:
            changed = (
                self.document_id != document_id
                or self.watch_generation != watch_generation
            )
            self.document_id = document_id
            self.watch_generation = watch_generation
            if changed:
                self.channels.clear()
                self.controls.clear()
                self.acks.clear()
            self.condition.notify_all()

    def register_client(
        self,
        *,
        client_id: str,
        document_id: str,
        watch_generation: str,
    ) -> str:
        self._validate_identity_fields(
            client_id,
            document_id,
            watch_generation,
        )
        with self.condition:
            self._require_active_identity_locked(
                document_id,
                watch_generation,
            )
            key = (client_id, document_id, watch_generation)
            channel = self.channels.get(key)
            if channel is None:
                channel = secrets.token_urlsafe(32)
                self.channels[key] = channel
                self.controls[key] = collections.deque(maxlen=64)
            return channel

    def authorize(
        self,
        *,
        client_id: str,
        document_id: str,
        watch_generation: str,
        channel: str,
    ) -> None:
        self._validate_identity_fields(
            client_id,
            document_id,
            watch_generation,
        )
        if not CHANNEL_PATTERN.fullmatch(channel):
            raise PreviewSyncError("Invalid preview channel.")
        with self.lock:
            self._authorize_locked(
                client_id=client_id,
                document_id=document_id,
                watch_generation=watch_generation,
                channel=channel,
            )

    @staticmethod
    def _validate_identity_fields(
        client_id: str,
        document_id: str,
        watch_generation: str,
    ) -> None:
        if not CLIENT_ID_PATTERN.fullmatch(client_id):
            raise PreviewSyncError("Invalid preview client identity.")
        if not DOCUMENT_ID_PATTERN.fullmatch(document_id):
            raise PreviewSyncError("Invalid preview document identity.")
        if not GENERATION_PATTERN.fullmatch(watch_generation):
            raise PreviewSyncError("Invalid preview watch generation.")

    def _require_active_identity_locked(
        self,
        document_id: str,
        watch_generation: str,
    ) -> None:
        if (
            self.document_id != document_id
            or self.watch_generation != watch_generation
        ):
            raise PreviewSyncError("Stale preview watch identity.")

    def _authorize_locked(
        self,
        *,
        client_id: str,
        document_id: str,
        watch_generation: str,
        channel: str,
    ) -> None:
        self._require_active_identity_locked(
            document_id,
            watch_generation,
        )
        expected = self.channels.get(
            (client_id, document_id, watch_generation)
        )
        if expected is None or not secrets.compare_digest(expected, channel):
            raise PreviewSyncError("Preview channel authorization failed.")

    def begin_run(
        self,
        *,
        package_sha256: str,
        client_id: str | None,
    ) -> PreviewRunBaseline:
        if not SHA256_PATTERN.fullmatch(package_sha256):
            raise PreviewSyncError("Invalid Office package fingerprint.")
        if client_id is not None and not CLIENT_ID_PATTERN.fullmatch(client_id):
            raise PreviewSyncError("Invalid initiating preview client.")
        with self.lock:
            if not self.document_id or not self.watch_generation:
                raise PreviewSyncError("The preview watch is not active.")
            return PreviewRunBaseline(
                document_id=self.document_id,
                watch_generation=self.watch_generation,
                mutation_sequence=self.mutation_sequence,
                package_sha256=package_sha256,
                client_id=client_id,
            )

    def observe_mutation(
        self,
        payload: dict[str, Any],
        *,
        document_id: str,
        watch_generation: str,
        package_sha256: str,
    ) -> WatchMutation | None:
        action = str(payload.get("action") or "")
        if action not in MUTATION_ACTIONS:
            return None
        if not SHA256_PATTERN.fullmatch(package_sha256):
            raise PreviewSyncError("Invalid Office package fingerprint.")
        version_value = payload.get("version", 0)
        base_value = payload.get("baseVersion")
        if isinstance(version_value, bool) or not isinstance(version_value, int):
            raise PreviewSyncError("Invalid OfficeCLI watch version.")
        if (
            base_value is not None
            and (isinstance(base_value, bool) or not isinstance(base_value, int))
        ):
            raise PreviewSyncError("Invalid OfficeCLI base watch version.")
        with self.condition:
            if (
                self.document_id != document_id
                or self.watch_generation != watch_generation
            ):
                return None
            self.mutation_sequence += 1
            mutation = WatchMutation(
                sequence=self.mutation_sequence,
                document_id=document_id,
                watch_generation=watch_generation,
                action=action,
                version=max(0, version_value),
                base_version=base_value,
                event_fingerprint=event_fingerprint(payload),
                package_sha256=package_sha256,
                observed_at=utc_iso(),
            )
            self.mutations.append(mutation)
            self.condition.notify_all()
            return mutation

    def acknowledge(
        self,
        *,
        client_id: str,
        document_id: str,
        watch_generation: str,
        channel: str,
        kind: str,
        version: int,
        package_sha256: str,
        event_fingerprint_value: str | None = None,
        dom_fingerprint: str | None = None,
        canonical_dom_fingerprint: str | None = None,
        control_id: str | None = None,
        viewport_path: str | None = None,
    ) -> PreviewAck:
        self._validate_identity_fields(
            client_id,
            document_id,
            watch_generation,
        )
        if not CHANNEL_PATTERN.fullmatch(channel):
            raise PreviewSyncError("Invalid preview channel.")
        if kind not in {"initial", "mutation", "refresh", "viewport"}:
            raise PreviewSyncError("Invalid preview acknowledgment kind.")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise PreviewSyncError("Invalid preview acknowledgment version.")
        if not SHA256_PATTERN.fullmatch(package_sha256):
            raise PreviewSyncError("Invalid preview package fingerprint.")
        if (
            event_fingerprint_value is not None
            and not FINGERPRINT_PATTERN.fullmatch(event_fingerprint_value)
        ):
            raise PreviewSyncError("Invalid preview event fingerprint.")
        if (
            dom_fingerprint is not None
            and not FINGERPRINT_PATTERN.fullmatch(dom_fingerprint)
        ):
            raise PreviewSyncError("Invalid preview DOM fingerprint.")
        if (
            canonical_dom_fingerprint is not None
            and not FINGERPRINT_PATTERN.fullmatch(
                canonical_dom_fingerprint
            )
        ):
            raise PreviewSyncError(
                "Invalid canonical preview DOM fingerprint."
            )
        if kind in {"mutation", "refresh"} and (
            dom_fingerprint is None
            or canonical_dom_fingerprint is None
            or not secrets.compare_digest(
                dom_fingerprint,
                canonical_dom_fingerprint,
            )
        ):
            raise PreviewSyncError(
                "Preview DOM did not match the canonical rendered content."
            )
        if kind == "initial" and dom_fingerprint is None:
            raise PreviewSyncError(
                "The initial preview DOM fingerprint is required."
            )
        if control_id is not None and not CONTROL_ID_PATTERN.fullmatch(control_id):
            raise PreviewSyncError("Invalid preview control identity.")
        normalized_path = None
        if viewport_path is not None:
            value = str(viewport_path)
            if (
                len(value) > 4096
                or not value.startswith("/")
                or any(ord(character) < 32 for character in value)
            ):
                raise PreviewSyncError("Invalid preview viewport path.")
            normalized_path = value
        ack = PreviewAck(
            kind=kind,
            client_id=client_id,
            document_id=document_id,
            watch_generation=watch_generation,
            version=version,
            event_fingerprint=event_fingerprint_value,
            package_sha256=package_sha256,
            dom_fingerprint=dom_fingerprint,
            control_id=control_id,
            viewport_path=normalized_path,
            acknowledged_at=utc_iso(),
            canonical_dom_fingerprint=canonical_dom_fingerprint,
        )
        with self.condition:
            self._authorize_locked(
                client_id=client_id,
                document_id=document_id,
                watch_generation=watch_generation,
                channel=channel,
            )
            self.acks.append(ack)
            self.condition.notify_all()
        return ack

    def matching_mutation(
        self,
        baseline: PreviewRunBaseline,
        package_sha256: str,
    ) -> WatchMutation | None:
        with self.lock:
            return next(
                (
                    item
                    for item in reversed(self.mutations)
                    if item.sequence > baseline.mutation_sequence
                    and item.document_id == baseline.document_id
                    and item.watch_generation == baseline.watch_generation
                    and item.package_sha256 == package_sha256
                ),
                None,
            )

    def associate_latest_mutation(
        self,
        baseline: PreviewRunBaseline,
        package_sha256: str,
    ) -> WatchMutation | None:
        """Bind the latest post-run watch event to a validated final package."""
        if not SHA256_PATTERN.fullmatch(package_sha256):
            raise PreviewSyncError("Invalid Office package fingerprint.")
        with self.condition:
            candidate_index = next(
                (
                    index
                    for index in range(len(self.mutations) - 1, -1, -1)
                    if self.mutations[index].sequence
                    > baseline.mutation_sequence
                    and self.mutations[index].document_id
                    == baseline.document_id
                    and self.mutations[index].watch_generation
                    == baseline.watch_generation
                ),
                None,
            )
            if candidate_index is None:
                return None
            candidate = self.mutations[candidate_index]
            if candidate.package_sha256 != package_sha256:
                candidate = dataclasses.replace(
                    candidate,
                    package_sha256=package_sha256,
                )
                self.mutations[candidate_index] = candidate
                self.condition.notify_all()
            return candidate

    def _matching_ack(
        self,
        *,
        baseline: PreviewRunBaseline,
        package_sha256: str,
        mutation: WatchMutation | None = None,
        control_id: str | None = None,
        kinds: set[str] | None = None,
    ) -> PreviewAck | None:
        if baseline.client_id is None:
            return None
        return next(
            (
                item
                for item in reversed(self.acks)
                if item.client_id == baseline.client_id
                and item.document_id == baseline.document_id
                and item.watch_generation == baseline.watch_generation
                and item.package_sha256 == package_sha256
                and (kinds is None or item.kind in kinds)
                and (
                    mutation is None
                    or (
                        item.version == mutation.version
                        and item.event_fingerprint
                        == mutation.event_fingerprint
                    )
                )
                and (control_id is None or item.control_id == control_id)
            ),
            None,
        )

    def _associate_mutation_ack(
        self,
        *,
        baseline: PreviewRunBaseline,
        package_sha256: str,
        mutation: WatchMutation,
    ) -> PreviewAck | None:
        if baseline.client_id is None:
            return None
        candidate_index = next(
            (
                index
                for index in range(len(self.acks) - 1, -1, -1)
                if self.acks[index].kind == "mutation"
                and self.acks[index].client_id == baseline.client_id
                and self.acks[index].document_id == baseline.document_id
                and self.acks[index].watch_generation
                == baseline.watch_generation
                and self.acks[index].version == mutation.version
                and self.acks[index].event_fingerprint
                == mutation.event_fingerprint
            ),
            None,
        )
        if candidate_index is None:
            return None
        candidate = self.acks[candidate_index]
        if candidate.package_sha256 != package_sha256:
            candidate = dataclasses.replace(
                candidate,
                package_sha256=package_sha256,
            )
            self.acks[candidate_index] = candidate
            self.condition.notify_all()
        return candidate

    def wait_for_mutation_confirmation(
        self,
        baseline: PreviewRunBaseline,
        package_sha256: str,
        *,
        timeout: float,
    ) -> PreviewConfirmation:
        deadline = time.monotonic() + max(0.0, timeout)
        with self.condition:
            while True:
                mutation = self.matching_mutation(baseline, package_sha256)
                if mutation is None:
                    mutation = self.associate_latest_mutation(
                        baseline,
                        package_sha256,
                    )
                if mutation is not None:
                    ack = self._matching_ack(
                        baseline=baseline,
                        package_sha256=package_sha256,
                        mutation=mutation,
                        kinds={"mutation"},
                    )
                    if ack is None:
                        ack = self._associate_mutation_ack(
                            baseline=baseline,
                            package_sha256=package_sha256,
                            mutation=mutation,
                        )
                    if ack is not None:
                        return PreviewConfirmation(
                            confirmed=True,
                            status="updated",
                            message="Preview updated",
                            mutation=mutation,
                            ack=ack,
                        )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return PreviewConfirmation(
                        confirmed=False,
                        status="waiting",
                        message="Preview update was not confirmed.",
                        mutation=mutation,
                    )
                self.condition.wait(remaining)

    def enqueue_control(
        self,
        *,
        client_id: str,
        document_id: str,
        watch_generation: str,
        action: str,
        package_sha256: str,
        version: int = 0,
        event_fingerprint_value: str | None = None,
    ) -> dict[str, Any]:
        self._validate_identity_fields(
            client_id,
            document_id,
            watch_generation,
        )
        if action not in {"full", "ogent-capture"}:
            raise PreviewSyncError("Invalid preview control action.")
        if not SHA256_PATTERN.fullmatch(package_sha256):
            raise PreviewSyncError("Invalid preview package fingerprint.")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise PreviewSyncError("Invalid preview control version.")
        if (
            event_fingerprint_value is not None
            and not FINGERPRINT_PATTERN.fullmatch(event_fingerprint_value)
        ):
            raise PreviewSyncError("Invalid preview event fingerprint.")
        with self.condition:
            self._require_active_identity_locked(
                document_id,
                watch_generation,
            )
            key = (client_id, document_id, watch_generation)
            if key not in self.channels:
                raise PreviewSyncError("The initiating preview client is unavailable.")
            self.control_sequence += 1
            control = {
                "seq": self.control_sequence,
                "action": action,
                "version": version,
                "_ogent": {
                    "control_id": secrets.token_hex(16),
                    "package_sha256": package_sha256,
                    "event_fingerprint": event_fingerprint_value,
                },
            }
            self.controls.setdefault(
                key,
                collections.deque(maxlen=64),
            ).append(control)
            self.condition.notify_all()
            return control

    def controls_after(
        self,
        *,
        client_id: str,
        document_id: str,
        watch_generation: str,
        channel: str,
        sequence: int,
        timeout: float,
    ) -> list[dict[str, Any]]:
        self._validate_identity_fields(
            client_id,
            document_id,
            watch_generation,
        )
        if not CHANNEL_PATTERN.fullmatch(channel):
            raise PreviewSyncError("Invalid preview channel.")
        key = (client_id, document_id, watch_generation)
        deadline = time.monotonic() + max(0.0, timeout)
        with self.condition:
            while True:
                self._authorize_locked(
                    client_id=client_id,
                    document_id=document_id,
                    watch_generation=watch_generation,
                    channel=channel,
                )
                values = [
                    dict(item)
                    for item in self.controls.get(key, ())
                    if int(item.get("seq", 0)) > sequence
                ]
                if values:
                    return values
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self.condition.wait(remaining)

    def wait_for_control_ack(
        self,
        baseline: PreviewRunBaseline,
        package_sha256: str,
        *,
        control_id: str,
        kind: str,
        timeout: float,
    ) -> PreviewConfirmation:
        deadline = time.monotonic() + max(0.0, timeout)
        with self.condition:
            while True:
                ack = self._matching_ack(
                    baseline=baseline,
                    package_sha256=package_sha256,
                    control_id=control_id,
                    kinds={kind},
                )
                if ack is not None:
                    return PreviewConfirmation(
                        confirmed=True,
                        status=(
                            "recovered"
                            if kind in {"refresh", "initial"}
                            else "captured"
                        ),
                        message=(
                            "Preview updated"
                            if kind in {"refresh", "initial"}
                            else "Preview position captured"
                        ),
                        ack=ack,
                        recovery=kind,
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return PreviewConfirmation(
                        confirmed=False,
                        status="waiting",
                        message="Preview recovery was not confirmed.",
                    )
                self.condition.wait(remaining)

    def wait_for_initial_ack(
        self,
        *,
        client_id: str,
        document_id: str,
        watch_generation: str,
        package_sha256: str,
        timeout: float,
    ) -> PreviewAck | None:
        if not CLIENT_ID_PATTERN.fullmatch(client_id):
            return None
        deadline = time.monotonic() + max(0.0, timeout)
        with self.condition:
            while True:
                ack = next(
                    (
                        item
                        for item in reversed(self.acks)
                        if item.kind == "initial"
                        and item.client_id == client_id
                        and item.document_id == document_id
                        and item.watch_generation == watch_generation
                        and item.package_sha256 == package_sha256
                    ),
                    None,
                )
                if ack is not None:
                    return ack
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.condition.wait(remaining)
