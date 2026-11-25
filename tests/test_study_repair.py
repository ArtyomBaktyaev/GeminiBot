import json
from pathlib import Path
import unittest
from study_clean import normalize_blocks
from study_markdown import parse_markdown

ROOT=Path(__file__).parent
TITLE='Производная функции: теория и практика'

class CleanupTests(unittest.TestCase):
    def test_real_regression_fixture(self):
        content=(ROOT/'fixtures/study_regression.md').read_text(encoding='utf-8')
        blocks=parse_markdown('```study\n{"target_pages":[5,6],"document_kind":"book"}\n```\n\n'+content)
        clean=normalize_blocks(blocks,TITLE)
        meta=json.loads(clean[0]['text'])
        self.assertEqual(meta['target_pages'],[5,6])
        self.assertEqual(meta['subject'],'Математический анализ')
        self.assertEqual(meta['goals'][0][0],'Определение')
        self.assertIsInstance(meta['outcomes'],str)
        self.assertEqual(len([b for b in clean if b.get('lang')=='study']),1)
        headings=[b['text'] for b in clean if b['t']=='h']
        self.assertEqual(len(headings),3,headings)
        self.assertEqual(headings[0],'Теоретические основы дифференцирования')
        self.assertEqual(len([b for b in clean if b.get('lang')=='plot']),3)
        self.assertEqual(sum(b['t']=='table' for b in clean),2)
        text='\n'.join(b.get('text','') for b in clean)
        self.assertNotIn('Привет!',text);self.assertNotIn('Если нужно что-то',text)
        self.assertIn('Теорема (Ферма):',text)
        self.assertIn('Задача 2',text)
        self.assertEqual(normalize_blocks(clean,TITLE),clean)
    def test_preserve_normal_prose_and_code(self):
        md='# Содержание вещества\n\nСодержание вещества зависит от температуры.\n\n```python\nprint("Привет!")\n```\n\nЕсли нужно найти максимум, сравним значения функции.'
        blocks=parse_markdown(md)
        self.assertEqual(normalize_blocks(blocks,'Химия'),blocks)
    def test_real_content_under_contents_heading(self):
        md='# Содержание\n\nЭтот раздел объясняет состав смеси.\n\n# Расчёт\n\nПроверим баланс.'
        blocks=parse_markdown(md)
        self.assertEqual(normalize_blocks(blocks,'Смесь'),blocks)

if __name__=='__main__': unittest.main()
