import unittest
from study_editorial import compact_chapters, align_cover, grouping_allowed

def h(t): return {'t':'h','lvl':1,'text':t}
def p(t): return {'t':'p','text':t}

class Editorial(unittest.TestCase):
    def setUp(self):
        self.body=[h('Введение в дифференциальное исчисление'),p('Определение'),h('Основные правила дифференцирования'),p('Правила'),h('Исследование функции'),p('Решение'),h('Графический анализ функции и производной'),{'t':'code','lang':'plot','text':'{}'},h('Задачи для самопроверки'),p('Задача'),h('Ответы и решения к задачам для самопроверки'),p('Ответ')]
    def test_merge_and_preserve(self):
        got=compact_chapters(self.body,[6,8],True)
        self.assertEqual(sum(b['t']=='h' and b['lvl']==1 for b in got),4)
        self.assertEqual([b for b in got if b['t']!='h'],[b for b in self.body if b['t']!='h'])
        self.assertEqual([b['text'] for b in got if b['t']=='h' and b['text'] not in ('Теория производных','Графики и задачи для самопроверки')],[b['text'] for b in self.body if b['t']=='h'])
        self.assertEqual(got[-2],self.body[-2])
        self.assertEqual(got,compact_chapters(got,[6,8],True))
        self.assertEqual(self.body[2]['lvl'],1)
    def test_respect_outline(self):
        for s in ('Глава 1: введение','Сделай 6 глав','Каждый раздел с новой страницы','Графики в отдельный раздел','Не меняй структуру'):
            self.assertFalse(grouping_allowed(s),s)
        self.assertTrue(grouping_allowed('Конспект на 6–8 страниц. Решения помести в отдельный раздел.'))
        self.assertEqual(compact_chapters(self.body,[6,8],False),self.body)
    def test_long_section_not_moved(self):
        self.body[1]['text']='a'*5000
        got=compact_chapters(self.body,[6,8],True)
        self.assertNotIn('Теория производных',[b.get('text') for b in got])
    def test_cover_idempotence(self):
        meta={'goals':[['Выдуманная глава','Описание']],'outcomes':'Проверка знаний'}
        got=compact_chapters(self.body,[6,8],True)
        align_cover(meta,got)
        self.assertNotIn('goals',meta)
        self.assertEqual(meta['outcomes'],'Разделов — 4, иллюстраций — 1. Проверка знаний')
        old=dict(meta);align_cover(meta,got);self.assertEqual(meta,old)

if __name__=='__main__': unittest.main()
