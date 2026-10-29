"""Independent v4 review. Real scraper/parse/hook; external services simulated."""
import ast
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

os.environ.setdefault('YESCAPA_EMAIL', 'review@example.invalid')
os.environ.setdefault('YESCAPA_PASSWORD', 'fictitious')
import yescapa_sheets as Y
import haven_emitter as E
import haven_sync as H
import test_main_real as T


def browser_simulation(yp, detail_fails=False, diagnostic_fails=False):
    """Fake only browser/API boundary. Run and detail/diagnostic methods stay real."""
    def request(url, **kwargs):
        archive = '?meta_state=archived' in url
        fails = diagnostic_fails if archive else detail_fails
        return NS(ok=not fails, status=503 if fails else 200,
                  json=lambda: {'count': 0} if archive else copy.deepcopy(T.RAW))
    page = NS(on=lambda *args: None, request=NS(get=request))
    browser = NS(new_context=lambda **kwargs: NS(new_page=lambda: page), close=lambda: None)
    pw = NS(chromium=NS(launch=lambda **kwargs: browser))
    def collect(page, meta, state):
        key = f'{meta}/{state}' if state else meta
        yp._api_counts[key] = 1 if key == 'confirmed' else 0
        if key == 'confirmed':
            yp._intercepted[T.RAW['id']] = copy.deepcopy(T.RAW)
    stack = contextlib.ExitStack()
    stack.enter_context(patch.object(Y, 'sync_playwright', lambda: contextlib.nullcontext(pw)))
    stack.enter_context(patch.object(yp, '_login', lambda page: None))
    stack.enter_context(patch.object(yp, '_collect_state', collect))
    return stack


class RealScraperReview(unittest.TestCase):
    def test_01_original_sheet_and_parser_functions_unchanged(self):
        old = ast.parse(Path('/reference/yescapa_sheets.reference.py').read_text(encoding='utf-8-sig'))
        new = ast.parse(Path('yescapa_sheets.py').read_text(encoding='utf-8-sig'))
        def top(tree, name):
            return next(n for n in tree.body if getattr(n, 'name', None) == name)
        for name in ('SheetsClient', 'parse_booking', 'direct_rows_to_bookings', '_fmt_date', '_col_letter'):
            with self.subTest(name=name):
                self.assertEqual(ast.dump(top(old, name)), ast.dump(top(new, name)))
        old_class, new_class = top(old, 'YescapaPlaywright'), top(new, 'YescapaPlaywright')
        for name in ('_login', '_collect_state', '_on_api_response', '_on_api_request'):
            with self.subTest(name=name):
                self.assertEqual(ast.dump(top(old_class, name)), ast.dump(top(new_class, name)))

    def test_02_real_run_resets_signals_and_blocks_failed_detail_or_diagnostic(self):
        yp = Y.YescapaPlaywright()
        with contextlib.redirect_stdout(io.StringIO()):
            for detail_fails, diagnostic_fails in ((True, False), (False, True), (False, False)):
                with self.subTest(detail=detail_fails, diagnostic=diagnostic_fails):
                    with browser_simulation(yp, detail_fails, diagnostic_fails):
                        raw = yp.run()
                    self.assertEqual(yp.detail_failures, int(detail_fails))
                    self.assertEqual(yp.archived_api_total, None if diagnostic_fails else 0)
                    envelope, errors = E.build_capture_envelope(
                        yp, [Y.parse_booking(r) for r in raw], '2026-10-29T12:00:00.123Z', 'real-scraper-v4-01')
                    self.assertEqual(envelope is not None, not (detail_fails or diagnostic_fails))

    def test_03_diagnostic_does_not_merge_rows_or_leak_headers(self):
        yp = Y.YescapaPlaywright()
        yp._api_counts = dict(T.COUNTS)
        yp._api_headers = {'Authorization': 'FICTITIOUS_REVIEW_SECRET'}
        yp._intercepted = {T.RAW['id']: copy.deepcopy(T.RAW)}
        before = copy.deepcopy(yp._intercepted)
        calls = []
        def get(url, **kwargs):
            calls.append((url, kwargs))
            return NS(ok=True, json=lambda: {'count': 0, 'results': [{'id': 99999}]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            yp._diagnostico_arquivadas(NS(request=NS(get=get)))
        self.assertEqual(before, yp._intercepted)
        self.assertEqual(yp.archived_api_total, 0)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('&state=', calls[0][0])
        self.assertIn('meta_state=archived', calls[0][0])
        self.assertNotIn('FICTITIOUS_REVIEW_SECRET', out.getvalue())

    def test_04_real_run_failed_detail_preserves_sheet_but_sends_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            yp = Y.YescapaPlaywright()
            sheets, calls = T.sheets_cls()
            with browser_simulation(yp, detail_fails=True):
                out, err, http, sent = T.run_main(yp, sheets, T.active(tmp))
            self.assertIsNone(err)
            self.assertEqual(http, 0)
            self.assertEqual(calls['updated'], [str(T.RAW['id']), 'DIRETA-0001'])

    def test_05_real_run_success_emits_same_37_sheet_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            yp = Y.YescapaPlaywright()
            sheets, calls = T.sheets_cls()
            with browser_simulation(yp):
                out, err, http, sent = T.run_main(yp, sheets, T.active(tmp))
            self.assertIsNone(err)
            self.assertEqual(http, 1)
            row = Y.parse_booking(T.RAW)
            self.assertEqual(sent[0], [list(row), [str(v or '') for v in row.values()]])
            self.assertEqual(len(sent[0][0]), 37)
            envelope = H.build_envelope([row], T.COUNTS, T.COUNTS, 0,
                                       '2026-10-29T12:00:00.123Z', 'real-scraper-consumer-v4')
            self.assertFalse(H.validate_envelope(envelope))
            Path('/evidence/real-scraper-envelope-fictitious.json').write_text(
                json.dumps(envelope, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    unittest.main(verbosity=2)
