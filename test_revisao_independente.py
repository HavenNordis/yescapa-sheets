"""Synthetic acceptance tests; external services mocked, real proposal hook.
Run original: 2 expected failures document remaining defects.
Run with the supplied minimal patch: all tests must pass.
"""
import contextlib
import copy
import io
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import haven_sync as H
import haven_queue as Q
import haven_emitter as E

COUNTS = dict.fromkeys(H.BY_STATE_KEYS, 0) | {'confirmed': 1}
AT = '2026-10-29T12:00:00.123Z'
BOOKING = dict.fromkeys(H.HEADERS, '') | {'ID':'900000000000001', 'Estado':'TO_COME',
 'Estado Meta':'confirmed', 'Data Início':'10/12/2026', 'Data Fim':'15/12/2026',
 'Nº Dias':'5', 'Hóspede Nome':'Cliente', 'Hóspede Apelido':'Ficticio', 'Viajantes':'2',
 'Veículo':'Ficticio', 'Matrícula':'QA-00-QA', 'Preço Hóspede':'301.0', 'Moeda':'EUR'}
ACTIVE = {'HAVEN_INGEST_ENABLED':'true', 'HAVEN_INGEST_URL':'https://haven.example.invalid/api/public/yescapa/snapshot', 'HAVEN_INGEST_TOKEN':'A'*43}
def envelope(eid='review-v3-event-0001'):
 return H.build_envelope([BOOKING], COUNTS, COUNTS, 0, AT, eid)
class Reply:
 status=200
 headers={}
 def __enter__(self): return self
 def __exit__(self,*args): pass
 def read(self,n=None): return b'{"ok":true,"status":"accepted"}'
class SheetsOK:
 def get_or_create_worksheet(self,*a): return object()
 def read_direct_bookings(self,*a): return []
 def update_bookings(self,*a): pass
 def update_log(self,*a): pass
class SheetsOpenFailure(SheetsOK):
 def get_or_create_worksheet(self,*a): raise OSError('synthetic sheets open failure')
def capture(**changes):
 return SimpleNamespace(**({'_api_counts':dict(COUNTS), '_intercepted':{1:{'meta_state':'confirmed','state':'TO_COME'}},
  'archived_api_total':0,'detail_failures':0,'run':lambda:[copy.deepcopy(BOOKING)]}|changes))
def load_proposal(yp=None,sheets=SheetsOK,parse=None):
 src=re.search(r'```python\n(.*?)\n```',Path(__file__).with_name('ligacao-captura.proposta.md').read_text(encoding='utf-8'),re.S).group(1)
 ns={}; exec(compile(src,'<reviewed-proposal-v3>','exec'),ns)
 ns.update({'YescapaPlaywright':lambda: yp or capture(), 'SheetsClient':sheets,
  'parse_booking':parse or (lambda b:copy.deepcopy(b)), 'GOOGLE_SHEET_NAME':'synthetic', 'WORKSHEET_NAME':'Reservas','LOGSHEET_NAME':'Log'})
 return ns

class IndependentReview(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
  self.env=patch.dict(os.environ,ACTIVE|{'HAVEN_QUEUE_DIR':self.tmp.name,'RAILWAY_VOLUME_MOUNT_PATH':self.tmp.name},clear=True)
  self.env.start(); self.addCleanup(self.env.stop)
 def test_01_sheets_open_failure_delivers_new_and_preserves_error(self):
  ns=load_proposal(sheets=SheetsOpenFailure)
  with patch.object(H.urllib.request,'urlopen',return_value=Reply()) as http, contextlib.redirect_stdout(io.StringIO()):
   with self.assertRaises(OSError): ns['main']()
  self.assertEqual(http.call_count,1)
 def test_02_systemexit_still_drains_old_event(self):
  Q.HavenQueue(Q.queue_path()).enqueue(envelope('old-systemexit-event'))
  def fail(): raise SystemExit('synthetic no rows')
  ns=load_proposal(capture(run=fail))
  with patch.object(H.urllib.request,'urlopen',return_value=Reply()) as http, contextlib.redirect_stdout(io.StringIO()):
   with self.assertRaises(SystemExit): ns['main']()
  self.assertEqual(http.call_count,1)
 def test_03_bad_new_counts_do_not_block_durable_queue(self):
  Q.HavenQueue(Q.queue_path()).enqueue(envelope('old-count-error-event'))
  ns=load_proposal(capture(_api_counts=COUNTS|{'waiting':'bad'}))
  with patch.object(H.urllib.request,'urlopen',return_value=Reply()) as http, contextlib.redirect_stdout(io.StringIO()): ns['main']()
  self.assertEqual(http.call_count,1)
 def test_04_failed_persistence_never_sends_new_capture(self):
  q=Q.HavenQueue(Q.queue_path()); sends=[]
  with patch.object(q,'_save',side_effect=OSError('synthetic write failure')):
   E.delivery_cycle(q,lambda e:sends.append(e),envelope())
  self.assertEqual(sends,[]); self.assertEqual(q.counts(),{}); self.assertFalse(Path(q.path).exists())
 def test_05_fatal_payload_time_code_survive_restart_and_requeue(self):
  for code,status in [('idempotency_conflict',409),('unauthorized',401),('invalid_matrix',422)]:
   with self.subTest(code=code):
    q=Q.HavenQueue(str(Path(self.tmp.name)/(code+'.json'))); ev=envelope('fatal-'+code)
    q.enqueue(ev); q.process(lambda _: {'http':status,'response':{'code':code}})
    reload=Q.HavenQueue(q.path); item=reload.get(ev['event_id'])
    self.assertEqual(item['envelope'],ev); self.assertEqual(item['source_snapshot_at'],AT); self.assertEqual(item['last_code'],code)
    self.assertTrue(reload.requeue(ev['event_id'])); self.assertEqual(Q.HavenQueue(q.path).get(ev['event_id'])['envelope'],ev)
 def test_06_quarantine_failure_preserves_original(self):
  p=Path(self.tmp.name)/'corrupt.json'; raw='[{"event_id":"old-pending"'; p.write_text(raw)
  with patch.object(Q.os,'replace',side_effect=OSError('synthetic quarantine failure')):
   with self.assertRaises(RuntimeError): Q.HavenQueue(str(p))
  self.assertEqual(p.read_text(),raw)
 def test_07_volume_checks_inside_outside_parent_and_link(self):
  root=Path(self.tmp.name); inside=root/'volume'; outside=root/'other'; inside.mkdir(); outside.mkdir()
  escape=inside/'escape'; escape.symlink_to(outside,target_is_directory=True)
  with patch.dict(os.environ,{'RAILWAY_VOLUME_MOUNT_PATH':str(inside),'HAVEN_QUEUE_DIR':str(inside/'queue')}):
   self.assertEqual(Q.queue_path(),str(inside/'queue'/'haven_fila.json'))
  for wrong in [outside, inside/'..'/'other',escape/'queue']:
   with self.subTest(wrong=str(wrong)),patch.dict(os.environ,{'RAILWAY_VOLUME_MOUNT_PATH':str(inside),'HAVEN_QUEUE_DIR':str(wrong)}):
    with self.assertRaises(RuntimeError): Q.queue_path()
 def test_08_enabled_without_volume_must_refuse(self):
  with patch.dict(os.environ,ACTIVE|{'HAVEN_QUEUE_DIR':str(Path(self.tmp.name)/'ephemeral')},clear=True):
   with self.assertRaises(RuntimeError): Q.queue_path()
 def test_09_parse_failure_must_drain_then_reraise_original(self):
  Q.HavenQueue(Q.queue_path()).enqueue(envelope('old-parse-failure-event'))
  err=TypeError('synthetic malformed captured record')
  def bad_parse(_): raise err
  ns=load_proposal(parse=bad_parse)
  output=io.StringIO(); seen=None
  with patch.object(H.urllib.request,'urlopen',return_value=Reply()) as http,contextlib.redirect_stdout(output):
   try: ns['main']()
   except TypeError as e: seen=e
  self.assertEqual(http.call_count,1)
  self.assertIs(seen,err,'Original parse failure must be preserved, not silently swallowed')
  self.assertNotIn('Concluído!',output.getvalue())
 def test_10_disabled_keeps_local_default_without_network(self):
  with patch.dict(os.environ,{'HAVEN_INGEST_ENABLED':'false'},clear=True),patch.object(H.urllib.request,'urlopen') as http:
   self.assertEqual(Q.queue_path(),'.haven_queue/haven_fila.json')
   self.assertTrue(H.send_envelope(envelope())['skipped']); self.assertEqual(http.call_count,0)

if __name__=='__main__': unittest.main(verbosity=2)
