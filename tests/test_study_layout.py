import json
import math
import unittest
from study_layout import requested_page_range, stamp_page_target, page_target, volume_issues, math_slot
from study_graph_marks import nice_ticks, markers
from study_markdown import parse_markdown
from study_figures import plot
from study_plot_expr import expand_plot

class LayoutTests(unittest.TestCase):
    def test_pages(self):
        self.assertEqual(requested_page_range("5–6 страниц A4"),(5,6))
        self.assertEqual(requested_page_range("5 страниц; допустима шестая страница"),(5,6))
        self.assertIsNone(requested_page_range("Найди производную"))
        content=stamp_page_target('```study\n{"subject":"Анализ"}\n```\n# Тема\nТекст', "5 страниц")
        self.assertEqual(page_target(parse_markdown(content)),(5,5))
        self.assertIn('Анализ',content)
        self.assertTrue(volume_issues("5 страниц",4000,[]))
    def test_formula_escaping(self):
        self.assertIn('&quot;',math_slot('"<script>',False,''))
        self.assertNotIn('<script>',math_slot('"<script>',False,''))
    def test_markers(self):
        data={'title':'Кубическая','domain':[-2.4,2.4],'series':[{'name':'f','expr':'x^3-3*x'}]}
        expanded=expand_plot(data)
        pts=markers(data,expanded['x'],expanded['series'][0]['y'])
        self.assertEqual(len(pts),5)
        for expected,(x,y,label) in zip([-math.sqrt(3),-1,0,1,math.sqrt(3)],pts):
            self.assertAlmostEqual(x,expected,places=4)
        self.assertIn('<circle',plot(data))
        self.assertIn('point-key',plot(data))
        self.assertEqual(nice_ticks(-2.4,2.4),[-2,-1,0,1,2])
    def test_explicit(self):
        data={'title':'Касательная','domain':[-2,2],'series':[{'name':'f','expr':'x*x'}],'points':[{'x':1,'label':'Касание'}]}
        self.assertIn('Касание (1; 1)',plot(data))

if __name__=='__main__': unittest.main()
