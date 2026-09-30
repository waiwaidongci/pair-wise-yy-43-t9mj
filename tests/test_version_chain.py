import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
class VersionChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.repo=Repository(str(Path(self.tmp.name)/"test.db"))
        self.service=Service(self.repo)
    def tearDown(self):
        self.repo.close(); self.tmp.cleanup()
    def _item(self, ref):
        return self.service.create_item({"title":ref,"description":ref,"severity":'major',"quantity":5,"threshold":10,"external_ref":ref},"creator",'observer')
    def _advance(self, item, targets):
        current=item
        for t in targets:
            current=self.service.transition(current["id"],t,current["version"],"reviewer",TRANSITION_ROLES[t][0])
        return current
    def _close_ready(self, item):
        item=self.service.get_item(item["id"],"viewer")
        self.service.add_record(item["id"],{"kind":"recovery","detail":"recovered","status":"closed","external_ref":"RC","expected_version":item["version"]},"recorder",'response_commander')
        item=self.service.get_item(item["id"],"viewer")
        self.service.add_record(item["id"],{"kind":"shoreline_monitoring","detail":"shoreline ok","status":"closed","external_ref":"SM","expected_version":item["version"]},"recorder",'response_commander')
        return self.service.get_item(item["id"],"viewer")
    def test_record_version_conflict(self):
        item=self._item("V-1")
        self.service.add_record(item["id"],{"kind":"evidence","detail":"first","external_ref":"A","expected_version":item["version"]},"recorder",'response_commander')
        with self.assertRaises(ConflictError):
            self.service.add_record(item["id"],{"kind":"evidence","detail":"late","external_ref":"B","expected_version":item["version"]},"recorder",'response_commander')
        item=self.service.get_item(item["id"],"viewer")
        self.service.add_record(item["id"],{"kind":"evidence","detail":"current","external_ref":"B","expected_version":item["version"]},"recorder",'response_commander')
        self.assertEqual(len(self.service.list_records(item["id"],"viewer")),2)
    def test_idempotent_retransmission_keeps_first_result(self):
        item=self._item("V-2")
        payload={"kind":"evidence","detail":"original","external_ref":"EV-1","expected_version":item["version"]}
        first=self.service.add_record(item["id"],payload,"recorder",'response_commander')
        item=self.service.get_item(item["id"],"viewer")
        payload["expected_version"]=item["version"]
        second=self.service.add_record(item["id"],payload,"recorder",'response_commander')
        self.assertEqual(first["id"],second["id"])
        self.assertEqual(first["detail"],second["detail"])
        self.assertEqual(len(self.service.list_records(item["id"],"viewer")),1)
        self.assertEqual(self.service.get_item(item["id"],"viewer")["version"],item["version"])
    def test_close_requires_recovery_and_shoreline_records(self):
        item=self._item("V-3")
        item=self._advance(item,STATES[1:-1])
        with self.assertRaises(ConflictError):
            self.service.transition(item["id"],STATES[-1],item["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
        item=self.service.get_item(item["id"],"viewer")
        self.service.add_record(item["id"],{"kind":"recovery","detail":"recovered","status":"closed","external_ref":"RC-3","expected_version":item["version"]},"recorder",'response_commander')
        item=self.service.get_item(item["id"],"viewer")
        with self.assertRaises(ConflictError):
            self.service.transition(item["id"],STATES[-1],item["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
        item=self.service.get_item(item["id"],"viewer")
        self.service.add_record(item["id"],{"kind":"shoreline_monitoring","detail":"shoreline ok","status":"closed","external_ref":"SM-3","expected_version":item["version"]},"recorder",'response_commander')
        item=self.service.get_item(item["id"],"viewer")
        closed=self.service.transition(item["id"],STATES[-1],item["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
        self.assertEqual(closed["status"],STATES[-1])
    def test_close_invalidated_by_reoil_and_reverified(self):
        item=self._item("V-4")
        item=self._advance(item,STATES[1:-1])
        item=self._close_ready(item)
        closed=self.service.transition(item["id"],STATES[-1],item["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
        self.assertEqual(closed["status"],STATES[-1])
        self.service.add_record(item["id"],{"kind":"oil_film_thickness","detail":"sheen observed","external_ref":"OIL-1","expected_version":closed["version"]},"recorder",'response_commander')
        reverted=self.service.get_item(item["id"],"viewer")
        self.assertEqual(reverted["status"],"assessing")
        with self.assertRaises(ConflictError):
            self.service.transition(reverted["id"],STATES[-1],reverted["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
        self.service.add_record(item["id"],{"kind":"recovery","detail":"recovered again","status":"closed","external_ref":"RC-RE","expected_version":reverted["version"]},"recorder",'response_commander')
        reverted=self.service.get_item(item["id"],"viewer")
        self.service.add_record(item["id"],{"kind":"shoreline_monitoring","detail":"shoreline rechecked","status":"closed","external_ref":"SM-RE","expected_version":reverted["version"]},"recorder",'response_commander')
        reverted=self.service.get_item(item["id"],"viewer")
        for t in STATES[2:]:
            reverted=self.service.transition(reverted["id"],t,reverted["version"],"reviewer",TRANSITION_ROLES[t][0])
        self.assertEqual(reverted["status"],STATES[-1])
        self.assertTrue(self.repo.verify_audit_chain())
        kinds=[e["action"] for e in self.service.audit("viewer",reverted["id"])]
        self.assertIn("close_invalidated",kinds)
    def test_plain_evidence_after_close_does_not_invalidate(self):
        item=self._item("V-5")
        item=self._advance(item,STATES[1:-1])
        item=self._close_ready(item)
        closed=self.service.transition(item["id"],STATES[-1],item["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
        self.service.add_record(closed["id"],{"kind":"evidence","detail":"late note","external_ref":"NOTE-1","expected_version":closed["version"]},"recorder",'response_commander')
        self.assertEqual(self.service.get_item(closed["id"],"viewer")["status"],STATES[-1])
    def test_batch_conflict_then_retry_is_idempotent(self):
        item=self._item("V-6")
        batch={"expected_version":99,"records":[
            {"kind":"recovery","detail":"r1","external_ref":"B1"},
            {"kind":"recovery","detail":"r2","external_ref":"B2"}]}
        with self.assertRaises(ConflictError):
            self.service.add_records_batch(item["id"],batch,"recorder",'response_commander')
        self.assertEqual(len(self.service.list_records(item["id"],"viewer")),0)
        self.assertEqual(self.service.get_item(item["id"],"viewer")["version"],1)
        batch["expected_version"]=1
        result=self.service.add_records_batch(item["id"],batch,"recorder",'response_commander')
        self.assertEqual(len(result["records"]),2)
        item=self.service.get_item(item["id"],"viewer")
        batch["expected_version"]=item["version"]
        again=self.service.add_records_batch(item["id"],batch,"recorder",'response_commander')
        self.assertEqual([r["id"] for r in again["records"]],[r["id"] for r in result["records"]])
        self.assertEqual(self.service.get_item(item["id"],"viewer")["version"],item["version"])
    def test_close_suspended_without_reverification(self):
        item=self._item("V-8")
        item=self._advance(item,STATES[1:-1])
        item=self._close_ready(item)
        closed=self.service.transition(item["id"],STATES[-1],item["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
        self.service.add_record(item["id"],{"kind":"shoreline_reoil","detail":"reoil","external_ref":"REOIL-8","expected_version":closed["version"]},"recorder",'response_commander')
        reverted=self.service.get_item(item["id"],"viewer")
        self.assertEqual(reverted["status"],"assessing")
        for t in STATES[2:-1]:
            reverted=self.service.transition(reverted["id"],t,reverted["version"],"reviewer",TRANSITION_ROLES[t][0])
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(reverted["id"],STATES[-1],reverted["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
        self.assertIn("重新核验",str(ctx.exception))
    def test_concurrent_record_submissions_only_current_version_wins(self):
        import threading
        item=self._item("V-9")
        results=[]
        barrier=threading.Barrier(2)
        def submit(ref):
            barrier.wait()
            try:
                self.service.add_record(item["id"],{"kind":"evidence","detail":"concurrent","external_ref":ref,"expected_version":1},"recorder",'response_commander')
                results.append(("ok",ref))
            except ConflictError:
                results.append(("conflict",ref))
        t1=threading.Thread(target=submit,args=("C1",))
        t2=threading.Thread(target=submit,args=("C2",))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(r[0] for r in results),["conflict","ok"])
        self.assertEqual(len(self.service.list_records(item["id"],"viewer")),1)
    def test_viewer_cannot_submit_records(self):
        item=self._item("V-7")
        with self.assertRaises(PermissionDenied):
            self.service.add_record(item["id"],{"kind":"evidence","detail":"x","expected_version":item["version"]},"recorder",'viewer')
if __name__=="__main__": unittest.main()
