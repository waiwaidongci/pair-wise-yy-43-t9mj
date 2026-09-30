import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
MAIN_FLOW=['assessing', 'containing', 'recovering', 'monitoring']
class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item({"title":"failure item","description":"failure scenarios","severity":'major',"quantity":5,"threshold":10,"external_ref":"FAIL-1"},"creator",'observer')
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def test_permission_version_duplicate_and_invariant(self):
        with self.assertRaises(PermissionDenied): self.service.transition(self.item["id"],STATES[1],1,"attacker","viewer")
        with self.assertRaises(ConflictError): self.service.transition(self.item["id"],STATES[1],99,"reviewer",TRANSITION_ROLES[STATES[1]][0])
        payload={"kind":"action","detail":"same reference","status":"open","external_ref":"DUP-1","expected_version":self.item["version"]}
        first=self.service.add_record(self.item["id"],payload,"recorder",'response_commander')
        # 同号重传沿用首次结果：不报错、不重复写、不推进版本
        again=self.service.add_record(self.item["id"],{"kind":"action","detail":"changed payload","status":"closed","external_ref":"DUP-1"},"recorder",'response_commander')
        self.assertTrue(again["replayed"])
        self.assertEqual(again["record"]["id"],first["record"]["id"])
        self.assertEqual(again["record"]["detail"],"same reference")
        self.assertEqual(len(self.service.list_records(self.item["id"],"viewer")),1)
        self.assertEqual(again["item"]["version"],first["item"]["version"])
        # 后到终端携带旧版本写新记录，收到冲突
        stale={"kind":"evidence","detail":"late write","status":"closed","field_ref":"LATE-1","expected_version":self.item["version"]}
        with self.assertRaises(ConflictError): self.service.add_record(self.item["id"],stale,"recorder",'response_commander')
        # 缺少版本号的新记录被拒绝
        with self.assertRaises(ValidationError):
            self.service.add_record(self.item["id"],{"kind":"evidence","detail":"no version","field_ref":"NOVER-1"},"recorder",'response_commander')
        current=self.service.get_item(self.item["id"],"viewer")
        self.service.add_record(current["id"],{"kind":"evidence","detail":"close blocker","status":"open","field_ref":"OPEN-1","expected_version":current["version"]},"recorder",'response_commander')
        current=self.service.get_item(self.item["id"],"viewer")
        for target in MAIN_FLOW: current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError): self.service.transition(current["id"],STATES[-1],current["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
if __name__=="__main__": unittest.main()
