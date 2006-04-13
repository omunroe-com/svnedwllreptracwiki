import unittest

from trac.wiki.tests import formatter, model, wikisyntax

def suite():

    suite = unittest.TestSuite()
    suite.addTest(formatter.suite())
    suite.addTest(model.suite())
    suite.addTest(wikisyntax.suite())
    return suite

if __name__ == '__main__':
    unittest.main(defaultTest='suite')
