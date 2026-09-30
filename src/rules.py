from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='溢油应急响应与任务追踪'; ENTITY='溢油事件'; ID_PREFIX='OS'
SEVERITIES=['minor', 'moderate', 'major', 'catastrophic']; STATES=['reported', 'assessing', 'containing', 'recovering', 'monitoring', 'closed']; TRANSITIONS={'reported': ['assessing'], 'assessing': ['containing'], 'containing': ['recovering'], 'recovering': ['monitoring'], 'monitoring': ['closed'], 'closed': []}; TRANSITION_ROLES={'assessing': ['response_commander'], 'containing': ['response_commander'], 'recovering': ['operations'], 'monitoring': ['operations'], 'closed': ['response_commander']}
CREATE_ROLES=set(['observer', 'response_commander']); RECORD_ROLES=set(['response_commander', 'operations']); AUDIT_ROLES=set(['response_commander', 'viewer']); VIEW_ROLES=set(['observer', 'response_commander', 'operations', 'viewer'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'major': 6.0, 'catastrophic': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'major': 8, 'catastrophic': 4}; TERMINAL_STATES=set(['closed'])
RECORD_KINDS=['evidence', 'action', 'recovery', 'shoreline_monitoring', 'oil_film_thickness', 'shoreline_reoil']
OBSERVATION_KINDS=['evidence', 'shoreline_monitoring', 'oil_film_thickness', 'shoreline_reoil']
CLOSE_REQUIRED_KINDS=['recovery', 'shoreline_monitoring']
STALE_CLOSE_KINDS=['oil_film_thickness', 'shoreline_reoil', 'recovery']
KIND_LABELS={'evidence': '证据', 'action': '行动', 'recovery': '回收', 'shoreline_monitoring': '岸线监测', 'oil_film_thickness': '油膜厚度', 'shoreline_reoil': '岸线复油'}
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def validate_record_kind(kind):
    if kind not in RECORD_KINDS: raise ValidationError("未知记录类型")
    return kind
def default_record_status(kind):
    return 'closed' if kind in OBSERVATION_KINDS else 'open'
def completion_blockers(target,open_records,missing_kinds=(),close_suspended=False):
    if target not in TERMINAL_STATES: return []
    blockers=[]
    if open_records>0: blockers.append("仍有未关闭事项")
    for kind in missing_kinds: blockers.append(f"缺少{KIND_LABELS.get(kind,kind)}记录")
    if close_suspended: blockers.append("关闭结论已失效，需重新核验")
    return blockers
def should_invalidate_close(status,kind):
    return status in TERMINAL_STATES and kind in STALE_CLOSE_KINDS
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
