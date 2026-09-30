from __future__ import annotations
from .domain import ConflictError, REOPEN_RECORD_KINDS, ValidationError
TITLE='溢油应急响应与任务追踪'; ENTITY='溢油事件'; ID_PREFIX='OS'
SEVERITIES=['minor', 'moderate', 'major', 'catastrophic']; STATES=['reported', 'assessing', 'containing', 'recovering', 'monitoring', 'review', 'closed']
# closed 没有任何允许的人工转出；关闭结论失效时由系统直接退回 review
TRANSITIONS={'reported': ['assessing'], 'assessing': ['containing'], 'containing': ['recovering'], 'recovering': ['monitoring'], 'monitoring': ['closed'], 'review': ['closed', 'monitoring'], 'closed': []}
TRANSITION_ROLES={'assessing': ['response_commander'], 'containing': ['response_commander'], 'recovering': ['operations'], 'monitoring': ['operations'], 'review': ['response_commander'], 'closed': ['response_commander']}
CREATE_ROLES=set(['observer', 'response_commander']); RECORD_ROLES=set(['response_commander', 'operations']); AUDIT_ROLES=set(['response_commander', 'viewer']); VIEW_ROLES=set(['observer', 'response_commander', 'operations', 'viewer'])
# 复核通过后允许重新关闭的记录类型
REVERIFICATION_KIND='reverification'
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'major': 6.0, 'catastrophic': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'major': 8, 'catastrophic': 4}; TERMINAL_STATES=set(['closed'])
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
def is_reopen_trigger(kind):
    """油膜厚度或岸线复油的新现场数据会使关闭结论失效。"""
    return kind in REOPEN_RECORD_KINDS
def reopen_reason(kind):
    if kind=='oil_slick_thickness': return "新的油膜厚度数据到达"
    if kind=='shoreline_reoil': return "新的岸线复油数据到达"
    return "新的现场监测数据到达"
def completion_blockers(target,open_records,needs_reverification=False):
    """关闭前置判定：未关闭记录、结论失效后未重新核验。"""
    blockers=[]
    if target in TERMINAL_STATES:
        if open_records>0: blockers.append("仍有未关闭事项")
        if needs_reverification: blockers.append("关闭结论已失效，须提交重新核验记录后方可关闭")
    return blockers
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
