from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ValidationError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES, VIEW_ROLES,
                    completion_blockers, default_record_status, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    should_invalidate_close, validate_record_kind, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    @staticmethod
    def _validate_record(raw: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValidationError("每条记录必须是对象")
        kind = validate_record_kind(require_text(raw.get("kind"), "kind", 100))
        detail = require_text(raw.get("detail"), "detail")
        status = raw.get("status")
        if status is None:
            status = default_record_status(kind)
        if status not in ("open", "closed"):
            raise ValidationError("status必须是open或closed")
        external_ref = raw.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        return {"kind": kind, "detail": detail, "status": status,
                "external_ref": external_ref}

    @staticmethod
    def _expected_version(value: Any, required: bool = False) -> Optional[int]:
        if value is None and not required:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValidationError("expected_version必须是正整数")
        return value

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str, expected_version: Optional[int] = None) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        record = self._validate_record(payload)
        if expected_version is None:
            expected_version = payload.get("expected_version")
        expected_version = self._expected_version(expected_version)
        item = self.repository.get_item(item_id)
        invalidate = should_invalidate_close(item["status"], record["kind"])
        stored, invalidated = self.repository.add_record(
            item_id, record["kind"], record["detail"], record["status"],
            record["external_ref"], actor, expected_version=expected_version,
            invalidate_close=invalidate)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": stored["id"], "kind": stored["kind"],
            "status": stored["status"], "external_ref": stored["external_ref"],
            "item_version": stored["item_version"],
        })
        if invalidated:
            self.repository.append_audit("close_invalidated", ENTITY, item_id, actor, {
                "record_id": stored["id"], "kind": stored["kind"],
            })
        return stored

    def add_records_batch(self, item_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = self._expected_version(payload.get("expected_version"), required=True)
        raw_records = payload.get("records")
        if not isinstance(raw_records, list) or not raw_records:
            raise ValidationError("records必须是非空数组")
        records: List[Dict[str, Any]] = [self._validate_record(raw) for raw in raw_records]
        stored, invalidated = self.repository.add_records_batch(
            item_id, expected_version, records, actor)
        self.repository.append_audit("record_batch", ENTITY, item_id, actor, {
            "count": len(stored), "expected_version": expected_version,
            "invalidated": invalidated,
        })
        if invalidated:
            self.repository.append_audit("close_invalidated", ENTITY, item_id, actor, {
                "count": len(stored),
            })
        return {"records": stored, "invalidated": invalidated}

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        expected_version = self._expected_version(expected_version, required=True)
        blockers = completion_blockers(
            target,
            self.repository.open_record_count(item_id),
            missing_kinds=self.repository.missing_required_kinds(item_id),
            close_suspended=self.repository.close_suspended(item_id),
        )
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
