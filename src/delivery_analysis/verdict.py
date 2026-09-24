from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .models import KeyMap, SrmRecord, SrmResult
from .srm import (
    as_objects,
    discover_key_map,
    get_path,
    key_inventory,
    normalize_request_id,
    urn_request_id,
)
from .timestamps import UTC, parse_srm_timestamp


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def extract_records(
    objects: list[dict[str, Any]],
    key_map: KeyMap,
    *,
    as_of: datetime,
    cutoff: datetime,
    states: tuple[str, ...],
) -> list[SrmRecord]:
    records: list[SrmRecord] = []
    for obj in objects:
        state = _stringify(get_path(obj, key_map.state))
        if state not in states:
            continue
        urn = _stringify(get_path(obj, key_map.urn))
        raw_updated = get_path(obj, key_map.updated_on)
        raw_text = _stringify(raw_updated)
        record = SrmRecord(urn=urn, state=state, updated_on=raw_text)
        if not urn:
            record.parse_error = "missing urn"
            records.append(record)
            continue
        if raw_text is None:
            record.parse_error = "missing timestamp"
            records.append(record)
            continue
        try:
            updated = parse_srm_timestamp(raw_text, tz=timezone.utc)
        except (ValueError, OverflowError, TypeError):
            record.parse_error = "unparseable timestamp"
            records.append(record)
            continue
        age_hours = (as_of - updated).total_seconds() / 3600.0
        record.updated_on_utc = updated
        record.age_hours = round(age_hours, 4)
        record.stale = updated < cutoff
        records.append(record)
    return records


def _reason(verdict: str, records: list[SrmRecord], stale_hours: float) -> str:
    hours = int(stale_hours) if stale_hours == int(stale_hours) else stale_hours
    if verdict == "NO_RECORDS":
        return (
            "No SUBMITTED or GRANTED DeliveryRequest records. Not an incident."
        )
    if verdict == "FRESH":
        return (
            f"All {len(records)} SUBMITTED/GRANTED records are within the last "
            f"{hours}h."
        )
    if verdict == "STALE":
        stale_count = sum(1 for rec in records if rec.stale)
        fresh_count = len(records) - stale_count
        return (
            f"At least one SUBMITTED/GRANTED record is older than {hours}h "
            f"({stale_count} stale, {fresh_count} fresh)."
        )
    return "SRM parse failure"


def evaluate_payload(
    payload: Any,
    *,
    as_of: datetime,
    stale_hours: float = 24.0,
    stale_mode: str = "any",
    states: tuple[str, ...] = ("SUBMITTED", "GRANTED"),
    url: str | None = None,
    notes: list[str] | None = None,
    environment: str | None = None,
    request_id: str | None = None,
) -> SrmResult:
    as_of = as_of.astimezone(UTC) if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    cutoff = as_of - timedelta(hours=stale_hours)
    objects = as_objects(payload)
    inventory = key_inventory(objects)
    collected_notes = list(notes or [])
    scoped_id = normalize_request_id(request_id)
    if scoped_id is not None:
        collected_notes.append(
            "request_id scope: only "
            f"strn:distribution:DeliveryRequest:{scoped_id}"
        )

    def build(
        verdict: str,
        reason: str,
        *,
        key_map: KeyMap | None = None,
        records: list[SrmRecord] | None = None,
        key_inventory_list: list[str] | None = None,
        extra_notes: list[str] | None = None,
    ) -> SrmResult:
        return SrmResult(
            verdict=verdict,  # type: ignore[arg-type]
            reason=reason,
            as_of=as_of,
            cutoff=cutoff,
            timezone="UTC",
            stale_hours=stale_hours,
            stale_mode=stale_mode,
            states=list(states),
            url=url,
            environment=environment,
            request_id=scoped_id,
            key_map=key_map or KeyMap(),
            records=records or [],
            key_inventory=key_inventory_list or [],
            notes=collected_notes + list(extra_notes or []),
        )

    if not objects:
        return build("NO_RECORDS", _reason("NO_RECORDS", [], stale_hours))

    key_map = discover_key_map(objects, states=states)
    if not key_map.state:
        return build(
            "SRM_ERROR",
            "Could not discover SRM state field",
            key_map=key_map,
            key_inventory_list=inventory,
            extra_notes=["redacted key inventory in key_inventory"],
        )

    records = extract_records(
        objects, key_map, as_of=as_of, cutoff=cutoff, states=states
    )

    if scoped_id is not None:
        records = [rec for rec in records if urn_request_id(rec.urn) == scoped_id]
        if not records:
            return build(
                "NO_RECORDS",
                "Requested DeliveryRequest id "
                f"{scoped_id} not found in SUBMITTED/GRANTED. Not an incident.",
                key_map=key_map,
                key_inventory_list=inventory,
                extra_notes=[f"requested id {scoped_id} not present in results"],
            )

    if not records:
        return build(
            "NO_RECORDS",
            _reason("NO_RECORDS", [], stale_hours),
            key_map=key_map,
            key_inventory_list=inventory,
        )

    parse_errors = [rec for rec in records if rec.parse_error]
    if parse_errors or not key_map.urn or not key_map.updated_on:
        detail = (
            parse_errors[0].parse_error
            if parse_errors
            else "missing urn or timestamp keys"
        )
        return build(
            "SRM_ERROR",
            f"SRM parse failure ({detail})",
            key_map=key_map,
            records=records,
            key_inventory_list=inventory,
        )

    stale_any = any(rec.stale for rec in records)
    verdict = "STALE" if stale_any else "FRESH"
    return build(
        verdict,
        _reason(verdict, records, stale_hours),
        key_map=key_map,
        records=records,
        key_inventory_list=inventory,
    )


def error_result(
    *,
    as_of: datetime,
    reason: str,
    stale_hours: float = 24.0,
    stale_mode: str = "any",
    states: tuple[str, ...] = ("SUBMITTED", "GRANTED"),
    url: str | None = None,
    notes: list[str] | None = None,
    environment: str | None = None,
    request_id: str | None = None,
) -> SrmResult:
    as_of = as_of.astimezone(UTC) if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    cutoff = as_of - timedelta(hours=stale_hours)
    return SrmResult(
        verdict="SRM_ERROR",
        reason=reason,
        as_of=as_of,
        cutoff=cutoff,
        timezone="UTC",
        stale_hours=stale_hours,
        stale_mode=stale_mode,
        states=list(states),
        url=url,
        environment=environment,
        request_id=normalize_request_id(request_id),
        notes=list(notes or []),
    )
