"""Install pinned OFL fonts at build time; never fetch fonts during a request."""
import hashlib
from pathlib import Path
from urllib.request import urlopen, Request
import time

BASE='https://raw.githubusercontent.com/google/fonts/6ebe737e44fd64ee736bbc6feed6136a6dbfd222/ofl/'
FILES={
 'Inter.ttf':('inter/Inter%5Bopsz%2Cwght%5D.ttf',876576,'047c92f6e2212473dc436020afed689527076d44'),
 'Inter-Italic.ttf':('inter/Inter-Italic%5Bopsz%2Cwght%5D.ttf',906596,'c177578428248f08eedcc60e292acc6f414c2167'),
 'Manrope.ttf':('manrope/Manrope%5Bwght%5D.ttf',164700,'75274da58537d6123b14f2cd0c355ad4681fc2b3'),
 'Inter-OFL.txt':('inter/OFL.txt',4377,'21f6aff961064c2e429f570995e446bcdd555422'),
 'Manrope-OFL.txt':('manrope/OFL.txt',4387,'e271172a9ed0cddc895aabb6509f1c7d880b492d'),
}

def valid(data,size,sha):
    return len(data)==size and hashlib.sha1(f'blob {len(data)}\0'.encode()+data).hexdigest()==sha

def install():
    root=Path(__file__).with_name('study_print')/'typefaces';root.mkdir(parents=True,exist_ok=True)
    errors=[]
    for name,(path,size,sha) in FILES.items():
        out=root/name
        if out.is_file() and valid(out.read_bytes(),size,sha): continue
        for attempt in range(2):
            try:
                with urlopen(Request(BASE+path,headers={'User-Agent':'GeminiStudy/6'}),timeout=12) as r:
                    data=r.read(size+1)
                if not valid(data,size,sha): raise ValueError('Font checksum mismatch')
                tmp=out.with_suffix(out.suffix+'.tmp');tmp.write_bytes(data);tmp.replace(out)
                print('Installed',name,flush=True);break
            except Exception as exc:
                if attempt==0: time.sleep(.5)
                else: errors.append(name);print('Font unavailable:',name,str(exc),flush=True)
        # Do not use an unverified previously downloaded file.
        if name in errors and out.is_file(): out.unlink()
    if errors: print('WARNING: using local/system fallback fonts for: '+', '.join(errors),flush=True)

if __name__=='__main__': install()
