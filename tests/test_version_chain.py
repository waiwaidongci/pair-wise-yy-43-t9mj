import tempfile, threading, unittest
from pathlib import Path
from src.domain import ConflictError, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import TRANSITION_ROLES
MAIN_FLOW=['assessing', 'containing', 'recovering', 'monitoring']
def to_monitoring(service, item):
    current=item
    for target in MAIN_FLOW:
        current=service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
    return current
class VersionChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item({"title":"chain item","description":"version chain","severity":'moderate',"quantity":3,"threshold":5,"external_ref":"CHAIN-1"},"creator",'observer')
    def tearDown(self): self.repo.close(); self.tmp.cleanup()

    def test_batch_atomic_retry_and_replay(self):
        version=self.item["version"]
        batch={"expected_version":version,"records":[
            {"kind":"evidence","detail":"a","status":"closed","field_ref":"B-1"},
            {"kind":"evidence","detail":"b","status":"closed","field_ref":"B-2"},
        ]}
        result=self.service.add_records(self.item["id"],batch,"recorder",'operations')
        self.assertEqual(len(result["records"]),2)
        self.assertEqual([r["item_version"] for r in result["records"]],[version+1,version+1])
        self.assertEqual(result["version"],version+1)
        # 整批只推进一个版本
        self.assertEqual(self.service.get_item(self.item["id"],"viewer")["version"],version+1)
        # 批次内含非法条目：整批不落库，原批次可原样重试
        bad={"expected_version":version+1,"records":[
            {"kind":"evidence","detail":"ok","status":"closed","field_ref":"C-1"},
            {"kind":"evidence","detail":"","status":"closed","field_ref":"C-2"},
        ]}
        with self.assertRaises(ValidationError):
            self.service.add_records(self.item["id"],bad,"recorder",'operations')
        self.assertEqual(len(self.service.list_records(self.item["id"],"viewer")),2)
        # 版本未被失败批次消耗，修正后原批次其余记录重试成功
        retry={"expected_version":version+1,"records":[
            {"kind":"evidence","detail":"a","status":"closed","field_ref":"B-1"},
            {"kind":"evidence","detail":"c","status":"closed","field_ref":"C-1"},
        ]}
        retried=self.service.add_records(self.item["id"],retry,"recorder",'operations')
        self.assertEqual(retried["replayed"],[True,False])
        self.assertEqual(retried["version"],version+2)
        self.assertEqual(len(self.service.list_records(self.item["id"],"viewer")),3)

    def test_two_terminals_only_current_version_accepted(self):
        v=self.item["version"]
        results=[]; errors=[]
        def terminal(ref):
            try:
                results.append(self.service.add_record(self.item["id"],
                    {"kind":"evidence","detail":"concurrent","status":"closed",
                     "field_ref":ref,"expected_version":v},
                    "recorder",'operations'))
            except ConflictError as exc: errors.append(exc)
        t1=threading.Thread(target=terminal,args=("T-1",))
        t2=threading.Thread(target=terminal,args=("T-2",))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(results),1)
        self.assertEqual(len(errors),1)
        self.assertEqual(len(self.service.list_records(self.item["id"],"viewer")),1)

    def test_new_slick_data_invalidates_closure_and_reopens(self):
        self.service.add_record(self.item["id"],{"kind":"recovery","detail":"done","status":"closed","field_ref":"R-1","expected_version":self.item["version"]},"recorder",'operations')
        current=to_monitoring(self.service,self.service.get_item(self.item["id"],"viewer"))
        closed=self.service.transition(current["id"],"closed",current["version"],"chief",'response_commander',conclusion="无残油，关闭")
        self.assertEqual(closed["status"],"closed")
        old_closure_id=closed["closure"]["id"]
        self.assertTrue(closed["closure"]["valid"])
        # 新的油膜厚度数据到达：结论立即失效、事件退回 review、关闭权限暂停
        reopened=self.service.add_record(closed["id"],
            {"kind":"oil_slick_thickness","detail":"岸边测得0.4mm油膜","status":"closed","field_ref":"OIL-9","expected_version":closed["version"]},
            "observer2",'operations')
        self.assertEqual(reopened["item"]["status"],"review")
        self.assertFalse(reopened["closure"]["valid"])
        self.assertEqual(reopened["closure"]["id"],old_closure_id)
        self.assertEqual(reopened["item"]["version"],closed["version"]+1)
        # 未重新核验前，再次关闭被拒绝
        with self.assertRaises(ConflictError):
            self.service.transition(closed["id"],"closed",reopened["item"]["version"],"chief",'response_commander')
        # 提交重新核验记录后允许重新关闭
        rev=self.service.add_record(closed["id"],
            {"kind":"reverification","detail":"复油已清除，复核通过","status":"closed",
             "field_ref":"REV-1","expected_version":reopened["item"]["version"]},
            "chief",'response_commander')
        reclosed=self.service.transition(closed["id"],"closed",rev["item"]["version"],"chief",'response_commander',conclusion="复核通过，重新关闭")
        self.assertEqual(reclosed["status"],"closed")
        self.assertTrue(reclosed["closure"]["valid"])
        self.assertNotEqual(reclosed["closure"]["id"],old_closure_id)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_shoreline_reoil_invalidates_closure(self):
        self.service.add_record(self.item["id"],{"kind":"recovery","detail":"done","status":"closed","field_ref":"R-1","expected_version":self.item["version"]},"recorder",'operations')
        current=to_monitoring(self.service,self.service.get_item(self.item["id"],"viewer"))
        closed=self.service.transition(current["id"],"closed",current["version"],"chief",'response_commander')
        reopened=self.service.add_record(closed["id"],
            {"kind":"shoreline_reoil","detail":"岸线复油","status":"closed","field_ref":"SHORE-9","expected_version":closed["version"]},
            "observer2",'operations')
        self.assertEqual(reopened["item"]["status"],"review")
        self.assertFalse(reopened["closure"]["valid"])
        actions=[e["action"] for e in self.service.audit("viewer",closed["id"])]
        self.assertIn("closure_invalidated",actions)

    def test_non_trigger_record_keeps_closure_valid(self):
        self.service.add_record(self.item["id"],{"kind":"recovery","detail":"done","status":"closed","field_ref":"R-1","expected_version":self.item["version"]},"recorder",'operations')
        current=to_monitoring(self.service,self.service.get_item(self.item["id"],"viewer"))
        closed=self.service.transition(current["id"],"closed",current["version"],"chief",'response_commander')
        outcome=self.service.add_record(closed["id"],
            {"kind":"photo","detail":"补充影像","field_ref":"PIC-1","expected_version":closed["version"]},
            "observer2",'operations')
        self.assertEqual(outcome["item"]["status"],"closed")
        self.assertTrue(outcome["closure"]["valid"])
if __name__=="__main__": unittest.main()
