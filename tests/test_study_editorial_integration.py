import json
import unittest
from study_clean import normalize_blocks
from study_layout import stamp_page_target, page_budget_hint
from study_markdown import parse_markdown

SOURCE = '''# Введение в дифференциальное исчисление
Определение: $f(x)$.
# Основные правила дифференцирования
$(x^2)'=2x$.
# Исследование функции
$$x^3-3x$$
# Графический анализ функции и производной
```plot
{"title":"Функция","domain":[-2,2],"series":[{"name":"f(x)","expr":"x**3-3*x"}]}
```
# Задачи для самопроверки
Пример: задача.
# Ответы и решения к задачам для самопроверки
Вывод: END_SENTINEL.
```
'''

class Integration(unittest.TestCase):
    def test_pipeline(self):
        s=stamp_page_target(SOURCE,'Конспект PDF на 6–8 страниц; решения помести в отдельный раздел.')
        blocks=normalize_blocks(parse_markdown(s))
        self.assertEqual(sum(b['t']=='h' and b['lvl']==1 for b in blocks),4)
        self.assertEqual(sum(b.get('lang')=='plot' for b in blocks),1)
        self.assertFalse(any(b['t']=='code' and not b['text'].strip() for b in blocks))
        self.assertTrue(any('END_SENTINEL' in b.get('text','') for b in blocks))
        self.assertEqual(blocks,normalize_blocks(blocks))
        self.assertEqual(json.loads(blocks[0]['text'])['target_pages'],[6,8])
        self.assertEqual(sum(b['t']=='h' and b['lvl']==1 and 'Ответы' in b['text'] for b in blocks),1)
    def test_explicit_structure(self):
        s=stamp_page_target(SOURCE,'Конспект на 6–8 страниц. Каждый раздел с новой страницы.')
        blocks=normalize_blocks(parse_markdown(s))
        self.assertEqual(sum(b['t']=='h' and b['lvl']==1 for b in blocks),6)
    def test_hint(self):
        self.assertIn('3–4',page_budget_hint('PDF 6–8 страниц'))

if __name__=='__main__': unittest.main()
