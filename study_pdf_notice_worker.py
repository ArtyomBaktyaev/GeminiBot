"""Isolate pypdf page-count imports from the long-lived application."""
from io import BytesIO
import sys


def main():
    from pypdf import PdfReader
    data = sys.stdin.buffer.read()
    with PdfReader(BytesIO(data)) as pdf:
        print(len(pdf.pages))


if __name__ == '__main__':
    main()
