from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text,
                     require_version)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours,
                    role_for_transition, validate_transition)


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

    def _normalize_entry(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValidationError("status必须是open或closed")
        # 现场单号：field_ref 为接口字段，external_ref 为兼容别名
        field_ref = payload.get("field_ref", payload.get("external_ref"))
        field_ref = require_text(field_ref, "field_ref", 100)
        return {"kind": kind, "detail": detail, "status": status,
                "field_ref": field_ref}

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        """单条写入，复用批量提交，保持原子与幂等语义。"""
        result = self._submit_records(item_id, [payload],
                                      payload.get("expected_version"), actor, role)
        result["record"] = result["records"][0]
        result["replayed"] = result["replayed"][0]
        return result

    def add_records(self, item_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        raw_entries = payload.get("records")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise ValidationError("records必须是非空数组")
        if len(raw_entries) > 500:
            raise ValidationError("单批最多500条记录")
        return self._submit_records(item_id, raw_entries,
                                    payload.get("expected_version"), actor, role)

    def _submit_records(self, item_id: int, raw_entries: List[Dict[str, Any]],
                        expected_version: Optional[int], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        # 先完整校验整个批次；任一条不合法则整批不写入，客户端可用原批次重试
        entries = [self._normalize_entry(raw) for raw in raw_entries]
        seen = set()
        for entry in entries:
            if entry["field_ref"] in seen:
                raise ValidationError(
                    f"批次内现场单号重复: {entry['field_ref']}")
            seen.add(entry["field_ref"])

        existing_refs = {
            r["external_ref"]
            for r in self.repository.list_records(item_id)
            if r["external_ref"] is not None
        }
        has_new = any(e["field_ref"] not in existing_refs for e in entries)
        if has_new:
            expected_version = require_version(expected_version)
        else:
            if expected_version is not None:
                require_version(expected_version)

        before = self.repository.get_item(item_id)
        result = self.repository.submit_records(
            item_id,
            [(e["kind"], e["detail"], e["status"], e["field_ref"]) for e in entries],
            expected_version if has_new else None,
            actor,
        )

        created = [r for r, replay in zip(result["records"], result["replayed"])
                   if not replay]
        if created:
            self.repository.append_audit("record", ENTITY, item_id, actor, {
                "record_ids": [r["id"] for r in created],
                "kinds": [r["kind"] for r in created],
                "field_refs": [r["external_ref"] for r in created],
                "batch_size": len(entries),
                "from_version": before["version"],
                "to_version": result["version"],
            })
        if result["reopened"]:
            self.repository.append_audit("closure_invalidated", ENTITY, item_id, "system", {
                "closure_id": result["invalidated_closure_id"],
                "record_id": result["reopen_record_id"],
                "kind": result["reopen_kind"],
                "reason": result["reopen_reason"],
                "from_status": "closed", "to_status": "review",
                "version": result["version"],
            })
        after = self.repository.get_item(item_id)
        result["item"] = self.enrich(after)
        result["closure"] = self._closure_view(item_id)
        return result

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str,
                   conclusion: Optional[str] = None) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        expected_version = require_version(expected_version)
        if target == "closed":
            conclusion = require_text(conclusion or "现场处置完成，准予关闭",
                                      "conclusion", 1000)
        else:
            conclusion = conclusion or ""

        open_records = self.repository.open_record_count(item_id)
        needs_reverification = False
        closure = self.repository.latest_closure(item_id)
        if target == "closed" and closure is not None and closure["valid"] == 0:
            trigger_id = closure["invalidated_record_id"]
            if not self.repository.has_reverification_after(item_id, trigger_id):
                needs_reverification = True
        blockers = completion_blockers(target, open_records, needs_reverification)
        if blockers:
            raise ConflictError("；".join(blockers))

        updated, closure_row = self.repository.transition_item(
            item_id, target, expected_version, actor, conclusion)
        audit_detail: Dict[str, Any] = {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        }
        if closure_row is not None:
            audit_detail["closure_id"] = closure_row["id"]
            audit_detail["conclusion"] = conclusion
        self.repository.append_audit("transition", ENTITY, item_id, actor, audit_detail)
        enriched = self.enrich(updated)
        enriched["closure"] = self._closure_view(item_id)
        return enriched

    def _closure_view(self, item_id: int) -> Optional[Dict[str, Any]]:
        closure = self.repository.latest_closure(item_id)
        if closure is None:
            return None
        view = dict(closure)
        view["valid"] = bool(closure["valid"])
        return view

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        item = self.enrich(self.repository.get_item(item_id))
        item["closure"] = self._closure_view(item_id)
        return item

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        items = [self.enrich(item) for item in self.repository.list_items(status)]
        for item in items:
            item["closure"] = self._closure_view(item["id"])
        return items

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
