import unittest
import ast
import tempfile
from pathlib import Path
from datetime import date
from unittest.mock import patch, Mock
import via_verde_delivery as delivery

class DeliveryTests(unittest.TestCase):
    def test_identity_keeps_separate_hours_and_exact_cents(self):
        p = {"date":date(2026,11,23),"hora":"10:01","location":"QA road","amount":"1.25"}
        first = delivery.passage_row("CE60LH", p)
        self.assertEqual(first["valor_cents"],125)
        self.assertEqual(first["record_id"],delivery.passage_row("CE-60-LH",p)["record_id"])
        self.assertNotEqual(first["record_id"],delivery.passage_row("CE60LH",dict(p,hora="11:01"))["record_id"])
        self.assertEqual(first["record_id"],delivery.passage_row("CE60LH",dict(p,amount="2.50"))["record_id"])
    def test_missing_time_and_fractional_cents_are_not_invented(self):
        p = {"date":date(2026,11,23),"hora":"","location":"QA road","amount":"1.25"}
        with self.assertRaises(ValueError): delivery.passage_row("CE60LH",p)
        with self.assertRaises(ValueError): delivery.passage_row("CE60LH",dict(p,hora="10:01",amount="1.255"))
    def test_disabled_does_not_touch_queue_or_network(self):
        with patch.dict(delivery.os.environ,{},clear=True), patch.object(delivery,"HavenQueue") as queue:
            self.assertEqual(delivery.deliver({},date.today(),date.today()),{"disabled":True})
            queue.assert_not_called()
    def test_volume_escape_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(delivery.os.environ,{"HAVEN_QUEUE_DIR":str(Path(root)/"../escape"),"RAILWAY_VOLUME_MOUNT_PATH":root},clear=True):
                with self.assertRaises(ValueError): delivery.queue_path()
    def test_persisted_retry_preserves_event_and_coverage(self):
        with tempfile.TemporaryDirectory() as root:
            env={"VV_HAVEN_ENABLED":"true","HAVEN_QUEUE_DIR":root,"RAILWAY_VOLUME_MOUNT_PATH":root}
            with patch.dict(delivery.os.environ,env,clear=True),patch.object(delivery,"send",return_value={"http":503}) as send:
                delivery.deliver({"CE-60-LH":[{"date":date(2026,11,23),"hora":"10:01","location":"QA road","amount":"1.25"}]},date(2026,10,19),date(2026,11,23))
                original=send.call_args.args[0]
                queue=delivery.HavenQueue(delivery.queue_path())
                self.assertEqual(queue.counts(),{"pending":1})
                item=queue.get(original["event_id"])
                self.assertEqual(item["envelope"],original)
                self.assertTrue(item["envelope"]["coverage"]["complete"])
                self.assertEqual(item["envelope"]["coverage"]["from"],"2026-10-19")
    def test_ambiguous_409_retries_original_event(self):
        from urllib.error import HTTPError
        env={"VV_HAVEN_ENABLED":"true","VV_HAVEN_URL":"https://desk.invalid/api/integracoes/via-verde","VV_HAVEN_TOKEN":"a"*43}
        with patch.dict(delivery.os.environ,env,clear=True),patch.object(delivery.urllib.request,"build_opener") as build:
            build.return_value.open.side_effect=HTTPError(env["VV_HAVEN_URL"],409,"conflict",{},None)
            self.assertEqual(delivery.send({})["http"],503)

def actual_function(name, extra):
    tree=ast.parse(Path("vv_runner.py").read_text(encoding="utf-8"))
    function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name==name)
    namespace=dict(extra)
    exec(compile(ast.Module(body=[function],type_ignores=[]),"vv_runner.py","exec"),namespace)
    return namespace[name]

class LoginTests(unittest.TestCase):
    def test_missing_filter_is_not_success(self):
        page = Mock()
        page.evaluate.return_value = False
        apply = actual_function("_set_angular", {})
        with self.assertRaisesRegex(RuntimeError,"ausente"): apply(page,"input","QA")
    def test_rejected_json_is_not_swallowed(self):
        requests=Mock()
        requests.Session.return_value.post.return_value.status_code=200
        requests.Session.return_value.post.return_value.json.return_value={"success":False}
        login=actual_function("_login",{"requests":requests,"VV_EMAIL":"qa@example.invalid","VV_PASSWORD":"synthetic"})
        with self.assertRaisesRegex(RuntimeError,"recusado"): login(Mock())
    def test_html_response_cannot_be_login_success(self):
        requests=Mock()
        requests.Session.return_value.post.return_value.status_code=200
        requests.Session.return_value.post.return_value.json.side_effect=ValueError()
        login=actual_function("_login",{"requests":requests,"VV_EMAIL":"qa@example.invalid","VV_PASSWORD":"synthetic"})
        with self.assertRaisesRegex(RuntimeError,"JSON"): login(Mock())

class SheetIdentityTests(unittest.TestCase):
    def test_separate_hours_and_replay_preserve_staff_fields(self):
        from datetime import datetime, timezone
        headers=["data","hora","local","matricula","valor","incluir","notas","criado_em","atualizado_em"]
        sheet=Mock()
        sheet.get_all_values.return_value=[headers,["23/11/2026","10:01","QA road","CE-60-LH","1.25","FALSE","Staff decision","",""]]
        upsert=actual_function("upsert_passages",{"get_vv_pass_ws":lambda _:sheet,"datetime":datetime,"LISBON_TZ":timezone.utc,"normalize_plate":lambda s:s.replace("-",""),"log":lambda _:None})
        p={"date":date(2026,11,23),"hora":"11:01","location":"QA road","amount":1.25}
        result=upsert(None,[dict(p,hora="10:01"),p,p],"CE-60-LH")
        self.assertEqual(len(result),1)
        self.assertEqual(sheet.append_row.call_count,1)
        self.assertTrue(all(c.args[1] not in (6,7) for c in sheet.update_cell.call_args_list))

if __name__ == "__main__":
    unittest.main()
