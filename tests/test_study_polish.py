import importlib.util
import math
import unittest
from study_polish import clean_point_label, coord, document_kind, stamp_profile, clean_summary
from study_graph_marks import markers
from study_plot_expr import expand_plot
from study_layout import page_target
from study_markdown import parse_markdown

class PolishTests(unittest.TestCase):
    def test_refined_roots_and_labels(self):
        spec={'title':'Кубическая','domain':[-2.4,2.4], 'series':[{'name':'f','expr':'x^3-3*x'}],
              'points':[{'x':-1.732,'label':'Нуль (-√3; 0)'},{'x':-1,'label':'Max (-1; 2)'},
                        {'x':0,'label':'Перегиб (0; 0)'},{'x':1,'label':'Min (1; -2)'}, {'x':1.732,'label':'Нуль (√3; 0)'}]}
        data=expand_plot(spec)
        pts=markers(spec,data['x'],data['series'][0]['y'])
        self.assertEqual(len(pts),5)
        self.assertAlmostEqual(pts[0][0],-math.sqrt(3),places=10)
        self.assertEqual(pts[0][1],0)
        self.assertEqual(pts[0][2],'Нуль')
        self.assertEqual(pts[1],(-1.0,2.0,'Максимум'))
        self.assertEqual(pts[3],(1.0,-2.0,'Минимум'))
        self.assertEqual(coord(1.764e-08),'0')
        self.assertEqual(clean_point_label('Точка касания (1; 1)'),'Точка касания')
    def test_no_false_zero(self):
        spec={'domain':[-2,2],'series':[{'name':'f','expr':'x*x+1'}], 'points':[{'x':1,'label':'Нуль'}]}
        data=expand_plot(spec)
        self.assertEqual(markers(spec,data['x'],data['series'][0]['y']),[(1.0,2.0,'Точка')])
    def test_profiles_preserve_target(self):
        self.assertEqual(document_kind('Сделай шпаргалку'),'cheat')
        self.assertEqual(document_kind('Реши задачу'),'solution')
        self.assertEqual(document_kind('Лабораторная работа'),'lab')
        self.assertEqual(document_kind('Учебное пособие'),'book')
        source='```study\n{"target_pages":[5,6],"subject":"Анализ"}\n```\n# Тема\nТекст'
        text=stamp_profile(source,'Учебное пособие')
        self.assertEqual(page_target(parse_markdown(text)),(5,6))
        self.assertIn('Анализ',text)
        self.assertEqual(clean_summary('Производная','Производная функции',['Теория','Задачи']),'Материал охватывает: Теория; Задачи.')

@unittest.skipUnless(importlib.util.find_spec('pypdf'), 'Optional PDF library not installed locally')
class PdfTextTests(unittest.TestCase):
    def test_searchable_text_and_links(self):
        from io import BytesIO
        from pypdf import PdfReader, PdfWriter
        from pypdf.annotations import Link
        from study_math_text import add_formula_text, PREFIX
        w=PdfWriter();w.add_blank_page(595,842)
        w.add_annotation(0,Link(rect=(40,200,300,230),url=PREFIX+'0'))
        w.add_annotation(0,Link(rect=(40,100,300,130),url='https://example.org'))
        w.add_named_destination('sec-1',0)
        buf=BytesIO();w.write(buf)
        tex=r'\frac{x^2}{3}'
        out=add_formula_text(buf.getvalue(),{'0':tex})
        r=PdfReader(BytesIO(out))
        self.assertIn(tex,r.pages[0].extract_text())
        self.assertIn('formula-source.txt',r.attachments)
        self.assertIn('sec-1',r.named_destinations)
        links=[str(x.get_object().get('/A',{}).get('/URI','')) for x in r.pages[0].get('/Annots',[])]
        self.assertEqual(links,['https://example.org'])

if __name__=='__main__': unittest.main()
