"""No-browser regression checks for the same-style v7.5 candidate."""
import json
import os
import threading
import unittest
from study_markdown_export import clean_markdown, reader_markdown, markdown_bytes, linear_math
from study_direct_chromium import PipeTransport, DirectPage


class MarkdownTests(unittest.TestCase):
    def test_metadata_figures_and_trailing_fence(self):
        source='```json\n{"subtitle":"Тема","subject":"Анализ","goals":[]}\n```\n# Задача\n```plot\n{"title":"График","domain":[-2,2],"series":[{"name":"f(x)","expr":"x**3-3*x","points":[{"x":-1,"label":"Максимум"}]}]}\n```\nОтвет: $f\'(x)=3x^2-3$.\n```ệp'
        out=reader_markdown(source)
        self.assertNotIn('"series"',out)
        self.assertNotIn('"subtitle"',out)
        self.assertNotIn('```ệp',out)
        self.assertIn('Максимум',out)
        self.assertIn('f′(x)=3x²−3',out)
    def test_math_preserves_grouping_and_symbols(self):
        self.assertEqual(linear_math(r'\frac{x^2-1}{x+1}'),'(x²−1)/(x+1)')
        self.assertEqual(linear_math(r'e^{-x}(3-x)'), 'e⁻ˣ(3−x)')
        self.assertEqual(linear_math(r'x_1=-2,\quad x_2=2'), 'x₁=−2, x₂=2')
        self.assertEqual(linear_math(r'x\in\mathbb{R}'), 'x∈ℝ')
        self.assertEqual(linear_math(r'\sqrt{2x+1}'), '√(2x+1)')
        self.assertEqual(linear_math(r'x^{12}'), 'x¹²')
    def test_complex_tex_never_silently_deleted(self):
        tex=r'\begin{cases}x&x>0\\0&x\le 0\end{cases}'
        self.assertIsNone(linear_math(tex))
        out=reader_markdown('$$'+tex+'$$')
        self.assertIn(tex,out)
        self.assertIn('```text',out)
    def test_normal_code_and_json_remain_exact(self):
        code='```python\nprice = "$10"\nx = 3**2\n```\n\n```json\n{"data":[1,2]}\n```'
        self.assertEqual(reader_markdown(code).strip(),code)
    def test_idempotence(self):
        text='## Формулы\n\n$$\\frac{x^2}{3}$$\n\n$f\'(x) = 2x$\n'
        self.assertEqual(reader_markdown(reader_markdown(text)),reader_markdown(text))
    def test_raw_formula_retained_for_pdf(self):
        original=r'Решение: $f(x)=x^2$'
        self.assertIn('$f(x)=x^2$',clean_markdown(original))
        self.assertIn('f(x)=x²',markdown_bytes(original).decode())


@unittest.skipUnless(os.name=='posix','CDP pipe is Linux-only')
class TransportTests(unittest.TestCase):
    def test_asset_pipe_matches_cdp_payload(self):
        import base64
        from pathlib import Path
        from tempfile import TemporaryDirectory
        data = bytes(range(256)) * 601
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'font.ttf'
            path.write_bytes(data)
            r, w = os.pipe()
            try:
                transport = PipeTransport(r, w)
                message = {'id': 7, 'sessionId': 'a', 'method': 'Fetch.fulfillRequest',
                           'params': {'requestId': 'b', 'responseCode': 200, 'body': path}}
                sender = threading.Thread(target=transport.send, args=(message,))
                sender.start()
                actual = transport.receive()
                sender.join()
                self.assertEqual(base64.b64decode(actual['params'].pop('body'), validate=True), data)
                self.assertEqual(actual['params'], {'requestId': 'b', 'responseCode': 200})
                self.assertEqual(actual['sessionId'], 'a')
            finally:
                os.close(r)
                os.close(w)

    def test_fragmented_null_delimited_messages(self):
        r,w=os.pipe()
        try:
            transport=PipeTransport(r,w)
            def produce():
                os.write(w,b'{"id":1,');os.write(w,b'"result":{}}\0{"id":2}\0')
            t=threading.Thread(target=produce);t.start()
            self.assertEqual(transport.receive(),{'id':1,'result':{}})
            self.assertEqual(transport.receive(),{'id':2});t.join()
        finally:os.close(r);os.close(w)
    def test_intercepted_only_allowlist(self):
        class Fake:
            def __init__(self):self.sent=[];self.events=[]
            def send(self,m):
                self.sent.append(m)
                result={'targetInfos':[{'type':'page','targetId':'page'}]} if m['method']=='Target.getTargets' else {'sessionId':'session'} if m['method']=='Target.attachToTarget' else {}
                self.events.append({'id':m['id'],'result':result})
            def receive(self):return self.events.pop(0)
        pipe=Fake();page=DirectPage(pipe)
        page.route('**/*',lambda r:r.abort())
        page.route('https://study.invalid/document',lambda r:r.fulfill(content_type='text/html',body='ok'))
        pipe.events.append({'method':'Fetch.requestPaused','params':{'requestId':'a','request':{'url':'https://study.invalid/document'}}})
        page._receive()
        pipe.events.append({'method':'Fetch.requestPaused','params':{'requestId':'b','request':{'url':'https://elsewhere.invalid/'}}})
        page._receive()
        self.assertEqual(pipe.sent[-2]['method'],'Fetch.fulfillRequest')
        self.assertEqual(pipe.sent[-1]['method'],'Fetch.failRequest')
        self.assertEqual(pipe.sent[-1]['params']['requestId'],'b')

if __name__=='__main__':unittest.main()
