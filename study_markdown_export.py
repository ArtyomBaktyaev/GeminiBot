"""Portable Markdown export; keep math and graph source intact."""
import json
import re


def markdown_bytes(source):
    text = source.replace('\r\n', '\n').replace('\r', '\n').lstrip('\ufeff').strip()
    # Only strip valid leading renderer metadata, never ordinary code blocks.
    match = re.match(r'^```study[ \t]*\n(.*?)\n```[ \t]*(?:\n|$)', text, re.S | re.I)
    if match:
        try:
            meta = json.loads(match.group(1))
        except (ValueError, TypeError):
            meta = None
        if isinstance(meta, dict):
            text = text[match.end():].lstrip('\n')
    return (text.rstrip() + '\n').encode('utf-8')
