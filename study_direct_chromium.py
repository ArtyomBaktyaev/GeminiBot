"""Direct CDP transport for the *same* Chromium print renderer. No Node driver."""
import base64
from contextlib import contextmanager
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import time
import select


_MISSING = object()


def executable_path():
    override = os.environ.get('STUDY_CHROMIUM')
    if override:
        return shutil.which(override) or override
    import playwright
    package = Path(playwright.__file__).parent / 'driver/package'
    config = json.loads((package / 'browsers.json').read_text(encoding='utf-8'))
    revision = next(str(x['revision']) for x in config['browsers'] if x['name'] == 'chromium-headless-shell')
    setting = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    if setting == '0' or (not setting and (package / '.local-browsers').is_dir()):
        cache = package / '.local-browsers'
    elif setting:
        cache = Path(setting)
    else:
        cache = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home()/'.cache'))) / 'ms-playwright'
    root = cache / ('chromium_headless_shell-' + revision)
    for name in ('chrome-headless-shell-linux64/chrome-headless-shell', 'chrome-headless-shell-linuxarm64/chrome-headless-shell', 'chrome-linux/headless_shell', 'chrome-headless-shell-linux64/headless_shell'):
        p = root / name
        if p.is_file(): return str(p)
    raise RuntimeError('Chromium headless shell is not installed at '+str(root))


class Route:
    def __init__(self, page, request_id):
        self.page, self.request_id = page, request_id
    def abort(self):
        self.page.call('Fetch.failRequest', requestId=self.request_id, errorReason='Aborted')
    def fulfill(self, *, content_type, body=None, path=None):
        if path is not None:
            encoded = Path(path)
        else:
            if isinstance(body, str):
                body = body.encode('utf-8')
            encoded = base64.b64encode(body).decode('ascii')
        self.page.call('Fetch.fulfillRequest', requestId=self.request_id, responseCode=200,
                       responseHeaders=[{'name':'Content-Type','value':content_type}],
                       body=encoded)


class Locator:
    def __init__(self, page, selector): self.page,self.selector=page,selector
    def evaluate_all(self, js, arg=_MISSING):
        query=json.dumps(self.selector)
        args='' if arg is _MISSING else ','+json.dumps(arg,ensure_ascii=False)
        return self.page.evaluate(f'() => ({js})([...document.querySelectorAll({query})]{args})')


class PipeTransport:
    """Chrome's NUL-delimited debugging pipe: no TCP listener, no driver."""
    def __init__(self, read_fd, write_fd):
        self.read_fd, self.write_fd = read_fd, write_fd
        self.pending=bytearray()

    def _write(self, data):
        data = memoryview(data)
        while data:
            n=os.write(self.write_fd,data)
            data=data[n:]

    def send(self, obj):
        params = obj.get('params', {})
        path = params.get('body')
        if not isinstance(path, Path):
            self._write(json.dumps(obj, ensure_ascii=False).encode('utf-8'))
            self._write(b'\0')
            return
        # CDP requires one base64 field. Write it incrementally on the same
        # pipe so neither the asset nor its JSON/base64 copies live in Python.
        envelope = {k: v for k, v in obj.items() if k != 'params'}
        headers = {k: v for k, v in params.items() if k != 'body'}
        with path.open('rb') as asset:
            prefix = json.dumps(envelope)[:-1] + ',"params":' + json.dumps(headers)[:-1] + ',"body":"'
            self._write(prefix.encode('utf-8'))
            while chunk := asset.read(48 * 1024):
                self._write(base64.b64encode(chunk))
            self._write(b'"}}\0')

    def receive(self):
        deadline=time.monotonic()+60
        while True:
            end=self.pending.find(b'\0')
            if end>=0:
                raw=bytes(self.pending[:end]); del self.pending[:end+1]
                return json.loads(raw)
            if not select.select([self.read_fd],[],[],max(0,deadline-time.monotonic()))[0]:
                raise TimeoutError('Chromium CDP response timed out')
            chunk=os.read(self.read_fd,65536)
            if not chunk: raise RuntimeError('Chromium CDP pipe closed')
            self.pending.extend(chunk)
            if len(self.pending)>64*1024*1024: raise RuntimeError('Chromium response exceeds limit')


class DirectPage:
    def __init__(self, transport):
        self.transport=transport; self.seq=0; self.responses={}; self.routes={}; self.session=None
        targets=self.call('Target.getTargets')['targetInfos']
        target=next((t['targetId'] for t in targets if t['type']=='page'),None)
        if target is None: target=self.call('Target.createTarget',url='about:blank')['targetId']
        self.session=self.call('Target.attachToTarget',targetId=target,flatten=True)['sessionId']
        self.call('Page.enable')
        self.call('Runtime.enable')
        self.call('Network.enable')
        self.call('Network.setBypassServiceWorker',bypass=True)
        self.call('Emulation.setDeviceMetricsOverride',width=1280,height=720,deviceScaleFactor=1,mobile=False)
        self.call('Fetch.enable',patterns=[{'urlPattern':'*','requestStage':'Request'}])
        self.call('Network.emulateNetworkConditions',offline=True,latency=0,downloadThroughput=0,uploadThroughput=0)
    def _receive(self):
        data=self.transport.receive()
        if 'id' in data: self.responses[data['id']]=data
        elif data.get('method')=='Fetch.requestPaused':
            p=data['params']; route=Route(self,p['requestId'])
            handler=self.routes.get(p['request']['url']) or self.routes.get('**/*')
            if handler: handler(route)
            else: route.abort()
    def call(self, method, **params):
        self.seq+=1; request=self.seq
        message={'id':request,'method':method,'params':params}
        if self.session: message['sessionId']=self.session
        self.transport.send(message)
        while request not in self.responses: self._receive()
        answer=self.responses.pop(request)
        if 'error' in answer: raise RuntimeError(f'CDP {method}: {answer["error"]}')
        return answer.get('result',{})
    def route(self, url, fn): self.routes[url]=fn
    def unroute(self, url): self.routes.pop(url,None)
    def emulate_media(self, *, media): self.call('Emulation.setEmulatedMedia',media=media)
    def evaluate(self, js, arg=_MISSING):
        expression='('+js+')('+('' if arg is _MISSING else json.dumps(arg,ensure_ascii=False))+')'
        result=self.call('Runtime.evaluate',expression=expression,awaitPromise=True,returnByValue=True,userGesture=True)
        if 'exceptionDetails' in result:
            e=result['exceptionDetails']; raise RuntimeError(e.get('exception',{}).get('description') or e.get('text','JS error'))
        return result.get('result',{}).get('value')
    def goto(self, url, *, wait_until='load', timeout=15000):
        result=self.call('Page.navigate',url=url)
        if result.get('errorText'): raise RuntimeError(result['errorText'])
        self.wait_for_function('location.href === '+json.dumps(url)+' && document.readyState === "complete"',timeout=timeout)
    def wait_for_function(self, expression, *, timeout=15000):
        end=time.monotonic()+timeout/1000
        while time.monotonic()<end:
            try:
                if self.evaluate('() => Boolean('+expression+')'): return
            except RuntimeError as exc:
                if 'context' not in str(exc).lower(): raise
            time.sleep(.025)
        raise TimeoutError(expression)
    def add_script_tag(self, *, url):
        self.evaluate('''url => new Promise((resolve,reject)=>{const s=document.createElement('script');s.src=url;s.onload=()=>resolve(true);s.onerror=()=>reject(Error('Script load failed'));document.head.append(s);})''',url)
    def locator(self, selector): return Locator(self,selector)
    def pdf(self, *, prefer_css_page_size, print_background, outline):
        # MathJax and the preceding print pass may leave reclaimable V8 objects.
        # Collect between passes while retaining the live document and all links.
        self.call('HeapProfiler.collectGarbage')
        r=self.call('Page.printToPDF',transferMode='ReturnAsStream',landscape=False,displayHeaderFooter=False,
                    headerTemplate='',footerTemplate='',printBackground=print_background,scale=1,paperWidth=8.5,paperHeight=11,
                    marginTop=0,marginBottom=0,marginLeft=0,marginRight=0,pageRanges='',preferCSSPageSize=prefer_css_page_size,
                    generateTaggedPDF=False,generateDocumentOutline=outline)
        stream=r['stream']; result=BytesIO()
        try:
            while True:
                chunk=self.call('IO.read',handle=stream,size=65536)
                result.write(base64.b64decode(chunk['data']) if chunk.get('base64Encoded') else chunk['data'].encode())
                if chunk.get('eof'): break
        finally: self.call('IO.close',handle=stream)
        return result.getvalue()


@contextmanager
def direct_page(*, args=()):
    # Use the same headless shell as Playwright, with the original renderer's
    # isolated profile and blocked external page requests. No debugging port.
    import fcntl
    with tempfile.TemporaryDirectory(prefix='study-chromium-') as profile:
        flags=['--headless','--no-sandbox','--disable-dev-shm-usage','--disable-background-networking',
               '--disable-component-update','--disable-extensions','--disable-default-apps','--disable-sync',
               '--no-first-run','--no-default-browser-check','--disable-breakpad','--disable-back-forward-cache',
               '--force-color-profile=srgb','--metrics-recording-only','--mute-audio','--hide-scrollbars',
               '--remote-debugging-pipe','--host-resolver-rules=MAP * ~NOTFOUND', '--user-data-dir='+profile]
        from_parent,to_child=os.pipe(); from_child,to_parent=os.pipe()
        # Duplicate away from Chrome's required descriptors 3/4 before exec.
        # No Python preexec_fn; the short launcher is replaced by Chromium.
        r=w=None; proc=None
        try:
            r=fcntl.fcntl(from_parent,fcntl.F_DUPFD,10)
            w=fcntl.fcntl(to_parent,fcntl.F_DUPFD,10)
            os.close(from_parent);from_parent=None
            os.close(to_parent);to_parent=None
            launcher='import os,sys; a,b=map(int,sys.argv[1:3]); os.dup2(a,3);os.dup2(b,4);os.close(a);os.close(b);os.execv(sys.argv[3],sys.argv[3:])'
            flags += ['--no-zygote', '--in-process-gpu', '--enable-features=NetworkServiceInProcess2',
                      '--js-flags=--optimize-for-size']
            proc=subprocess.Popen([sys.executable,'-c',launcher,str(r),str(w),executable_path(),*flags,*args,'about:blank'],
                                  pass_fds=(r,w),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            os.close(r);r=None;os.close(w);w=None
            transport=PipeTransport(from_child,to_child)
            page=DirectPage(transport)
            try:
                yield page
            finally:
                try:
                    transport.send({'id':page.seq+1,'method':'Browser.close'})
                except (OSError,RuntimeError):
                    pass
        finally:
            for fd in (r,w,from_parent,to_parent,from_child,to_child):
                if fd is not None:
                    try: os.close(fd)
                    except OSError: pass
            if proc is not None:
                try: proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try: proc.wait(timeout=3)
                    except subprocess.TimeoutExpired: proc.kill();proc.wait(timeout=5)
