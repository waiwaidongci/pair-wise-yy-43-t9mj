import tempfile, unittest
from pathlib import Path
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
MAIN_FLOW=['assessing', 'containing', 'recovering', 'monitoring', 'closed']
class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def test_complete_workflow_and_audit(self):
        item=self.service.create_item({"title":"workflow item","description":"complete business flow","severity":'major',"quantity":12,"threshold":6,"external_ref":"WF-1"},"creator",'observer')
        self.assertEqual(item["status"],STATES[0])
        # 记录写入纳入版本链，须携带当前版本
        record_result=self.service.add_record(item["id"],{"kind":"evidence","detail":"evidence registered","status":"closed","external_ref":"EV-1","expected_version":item["version"]},"recorder",'response_commander')
        self.assertEqual(record_result["item"]["version"],item["version"]+1)
        current=record_result["item"]
        for target in MAIN_FLOW:
            current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"],STATES[-1])
        self.assertTrue(current["closure"]["valid"])
        self.assertEqual(len(self.service.list_records(current["id"],"viewer")),1)
        events=self.service.audit("viewer",current["id"]); self.assertGreaterEqual(len(events),len(MAIN_FLOW)+2); self.assertTrue(self.repo.verify_audit_chain())
if __name__=="__main__": unittest.main()
