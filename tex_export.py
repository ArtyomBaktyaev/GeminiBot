"""Compile the same UTF-8 source returned by the LaTeX download."""
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import Semaphore

_slot = Semaphore(1)


def compile_tex(source):
    local = Path(__file__).resolve().parent / ".tinytex/bin/x86_64-linux/pdflatex"
    compiler = str(local) if local.is_file() else shutil.which("pdflatex")
    if not compiler:
        raise RuntimeError("Компилятор LaTeX отсутствует на сервере. Установите зависимости экспорта при сборке.")
    with _slot, TemporaryDirectory() as folder:
        root = Path(folder)
        (root / "answer.tex").write_bytes(source)
        env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOME", "TEMP", "TMP") if k in os.environ}
        env.update(openin_any="p", openout_any="p", shell_escape="f")
        args = [compiler, "-no-shell-escape", "-interaction=batchmode", "-halt-on-error"]
        if os.name == "nt":
            args.append("--disable-installer")
        for _ in range(2):
            result = subprocess.run([*args, "answer.tex"], cwd=root, env=env, timeout=40,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode:
                raise ValueError("LaTeX не смог собрать документ. Исходник доступен по кнопке LaTeX (.tex).")
        return (root / "answer.pdf").read_bytes()
