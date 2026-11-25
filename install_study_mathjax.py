"""Install the pinned MathJax SVG engine at build time, not per document."""
from pathlib import Path
import io
import json
import tarfile
from urllib.request import urlopen
import hashlib
import base64

VERSION = "3.2.1"
DEST = Path(__file__).resolve().parent / "study_print" / "mathjax-svg.js"

def install():
    url = f"https://registry.npmjs.org/mathjax-full/{VERSION}"
    with urlopen(url, timeout=30) as response:
        package = json.load(response)
    assert package["name"] == "mathjax-full" and package["version"] == VERSION
    dist = package["dist"]
    expected_url = f"https://registry.npmjs.org/mathjax-full/-/mathjax-full-{VERSION}.tgz"
    if dist["tarball"] != expected_url:
        raise RuntimeError("Unexpected MathJax package URL")
    with urlopen(expected_url, timeout=90) as response:
        data = response.read(60_000_001)
    if len(data) > 60_000_000:
        raise RuntimeError("MathJax archive too large")
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    if integrity not in dist["integrity"].split():
        raise RuntimeError("MathJax package integrity check failed")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        entry = archive.getmember("package/es5/tex-svg-full.js")
        if not entry.isfile() or not 100_000 < entry.size < 8_000_000:
            raise RuntimeError("Unexpected MathJax bundle")
        script = archive.extractfile(entry).read()
        license_entry = archive.getmember("package/LICENSE")
        license_text = archive.extractfile(license_entry).read()
    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_bytes(script)
    DEST.with_name("mathjax-LICENSE.txt").write_bytes(license_text)
    print(f"MathJax {VERSION} installed ({len(script)} bytes)")

if __name__ == "__main__":
    install()
