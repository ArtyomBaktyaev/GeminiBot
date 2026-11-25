import unittest
from html import unescape
from study_typography import expression_tex, series_label, rich_figure_label, point_label
from install_study_fonts import valid
import hashlib

class TypographyTests(unittest.TestCase):
    def test_expression(self):
        self.assertEqual(expression_tex('x**3-3*x'),'x^{3}-3x')
        self.assertEqual(expression_tex('3*x**2-3'),'3x^{2}-3')
        self.assertEqual(expression_tex('2*x-1'),'2x-1')
        self.assertEqual(expression_tex('1/(x+1)'),r'\frac{1}{x+1}')
        self.assertEqual(expression_tex('sqrt(x+1)'),r'\sqrt{x+1}')
        self.assertEqual(expression_tex('(-x)**2'),r'\left(-x\right)^{2}')
        with self.assertRaises(ValueError): expression_tex('__import__("os")')

    def test_series_from_actual_expression(self):
        html=unescape(series_label("f'(x)=99",'3*x**2-3'))
        self.assertIn('data-tex="f\'(x)=3x^{2}-3"',html)
        self.assertNotIn('99',html)
        self.assertIn('Касательная',series_label('Касательная','2*x-1'))
        self.assertIn('study-math',series_label('y=x^2'))
        self.assertEqual(series_label('Наблюдения'),'Наблюдения')

    def test_caption_and_points(self):
        self.assertIn('data-tex="f(x)=x^{3}-3x"',unescape(rich_figure_label('График функции f(x) = x^3 - 3x с нулями')))
        self.assertIn('study-math',rich_figure_label('График $x^2$'))
        self.assertIn('&lt;script&gt;',rich_figure_label('<script>'))
        self.assertNotIn('<script>',series_label('<script>','x'))
        self.assertIn(r'\left(-1;\,2\right)',unescape(point_label('Максимум',-1,2)))

    def test_font_checksum(self):
        data=b'abc'; sha=hashlib.sha1(b'blob 3\0'+data).hexdigest()
        self.assertTrue(valid(data,3,sha))
        self.assertFalse(valid(b'abd',3,sha))

    def test_actual_plot(self):
        from study_figures import plot
        html=unescape(plot({'title':'График','domain':[-2,2],'series':[{'name':'f(x)=x^3-3x','expr':'x**3-3*x'},{'name':"f'(x)=3x^2-3",'expr':'3*x**2-3'}]}))
        self.assertIn('data-tex="f(x)=x^{3}-3x"',html)
        self.assertIn('data-tex="f\'(x)=3x^{2}-3"',html)
        self.assertIn('class="axis-var"',html)
        self.assertIn('<polyline',html)

if __name__=='__main__': unittest.main()
