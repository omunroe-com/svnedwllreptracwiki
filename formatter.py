# -*- coding: utf-8 -*-
#
# Copyright (C) 2003-2006 Edgewall Software
# Copyright (C) 2003-2005 Jonas Borgström <jonas@edgewall.com>
# Copyright (C) 2004-2005 Christopher Lenz <cmlenz@gmx.de>
# Copyright (C) 2005-2006 Christian Boos <cboos@neuf.fr>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at http://trac.edgewall.com/license.html.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at http://projects.edgewall.com/trac/.
#
# Author: Jonas Borgström <jonas@edgewall.com>
#         Christopher Lenz <cmlenz@gmx.de>

import re
import os
import urllib

from StringIO import StringIO

from trac.core import *
from trac.mimeview import *
from trac.wiki.api import WikiSystem, IWikiChangeListener, IWikiMacroProvider
from trac.util import shorten_line, to_unicode
from trac.util.markup import escape, Markup, Element, html

__all__ = ['wiki_to_html', 'wiki_to_oneliner', 'wiki_to_outline', 'Formatter' ]


def system_message(msg, text):
    return """<div class="system-message">
 <strong>%s</strong>
 <pre>%s</pre>
</div>
""" % (escape(msg), escape(text))


class WikiProcessor(object):

    _code_block_re = re.compile('^<div(?:\s+class="([^"]+)")?>(.*)</div>$')

    def __init__(self, env, name):
        self.env = env
        self.name = name
        self.error = None

        builtin_processors = {'html': self._html_processor,
                              'default': self._default_processor,
                              'comment': self._comment_processor}
        self.processor = builtin_processors.get(name)
        if not self.processor:
            # Find a matching wiki macro
            for macro_provider in WikiSystem(self.env).macro_providers:
                if self.name in list(macro_provider.get_macros()):
                    self.processor = self._macro_processor
                    break
        if not self.processor:
            # Find a matching mimeview renderer
            from trac.mimeview.api import MIME_MAP
            if MIME_MAP.has_key(self.name):
                self.name = MIME_MAP[self.name]
                self.processor = self._mimeview_processor
            elif self.name in MIME_MAP.values():
                self.processor = self._mimeview_processor
            else:
                self.processor = self._default_processor
                self.error = 'No macro named [[%s]] found' % name

    def _comment_processor(self, req, text):
        return ''

    def _default_processor(self, req, text):
        return '<pre class="wiki">' + escape(text) + '</pre>\n'

    def _html_processor(self, req, text):
        from HTMLParser import HTMLParseError
        try:
            return Markup(text).sanitize()
        except HTMLParseError, e:
            self.env.log.warn(e)
            return system_message('HTML parsing error: %s' % escape(e.msg),
                                  text.splitlines()[e.lineno - 1].strip())

    def _macro_processor(self, req, text):
        for macro_provider in WikiSystem(self.env).macro_providers:
            if self.name in list(macro_provider.get_macros()):
                self.env.log.debug('Executing Wiki macro %s by provider %s'
                                   % (self.name, macro_provider))
                return macro_provider.render_macro(req, self.name, text)

    def _mimeview_processor(self, req, text):
        return Mimeview(self.env).render(req, self.name, text)

    def process(self, req, text, in_paragraph=False):
        if self.error:
            return system_message(Markup('Error: Failed to load processor '
                                         '<code>%s</code>', self.name),
                                  self.error)
        text = self.processor(req, text)
        if in_paragraph:
            content_for_span = None
            interrupt_paragraph = False
            if isinstance(text, Element):
                tagname = text.tagname.lower()
                if tagname == 'div':
                    class_ = text.attr.get('class_', '')
                    if class_ and 'code' in class_:
                        content_for_span = text.children
                    else:
                        interrupt_paragraph = True
                elif tagname == 'table':
                    interrupt_paragraph = True
            else:
                match = re.match(self._code_block_re, text)
                if match:
                    if match.group(1) and 'code' in match.group(1):
                        content_for_span = match.group(2)
                    else:
                        interrupt_paragraph = True
                elif text.startswith('<table'):
                    interrupt_paragraph = True
            if content_for_span:
                text = html.SPAN(class_='code-block')[content_for_span]
            elif interrupt_paragraph:
                text = "</p>%s<p>" % to_unicode(text)
        return to_unicode(text)


class Formatter(object):
    flavor = 'default'

    # Some constants used for clarifying the Wiki regexps:

    BOLDITALIC_TOKEN = "'''''"
    BOLD_TOKEN = "'''"
    ITALIC_TOKEN = "''"
    UNDERLINE_TOKEN = "__"
    STRIKE_TOKEN = "~~"
    SUBSCRIPT_TOKEN = ",,"
    SUPERSCRIPT_TOKEN = r"\^"
    INLINE_TOKEN = "`"
    STARTBLOCK_TOKEN = r"\{\{\{"
    STARTBLOCK = "{{{"
    ENDBLOCK_TOKEN = r"\}\}\}"
    ENDBLOCK = "}}}"
    
    LINK_SCHEME = r"[\w.+-]+" # as per RFC 2396
    INTERTRAC_SCHEME = r"[a-zA-Z.+-]*?" # no digits (support for shorthand links)

    QUOTED_STRING = r"'[^']+'|\"[^\"]+\""

    SHREF_TARGET_FIRST = r"[\w/?!#@]"
    SHREF_TARGET_MIDDLE = r"(?:\|(?=[^|\s])|[^|<>\s])"
    SHREF_TARGET_LAST = r"[a-zA-Z0-9/=]" # we don't want "_"

    LHREF_RELATIVE_TARGET = r"[/.][^\s[\]]*"


    # Rules provided by IWikiSyntaxProviders will be inserted,
    # between _pre_rules and _post_rules

    _pre_rules = [
        # Font styles
        r"(?P<bolditalic>%s)" % BOLDITALIC_TOKEN,
        r"(?P<bold>%s)" % BOLD_TOKEN,
        r"(?P<italic>%s)" % ITALIC_TOKEN,
        r"(?P<underline>!?%s)" % UNDERLINE_TOKEN,
        r"(?P<strike>!?%s)" % STRIKE_TOKEN,
        r"(?P<subscript>!?%s)" % SUBSCRIPT_TOKEN,
        r"(?P<superscript>!?%s)" % SUPERSCRIPT_TOKEN,
        r"(?P<inlinecode>!?%s(?P<inline>.*?)%s)" \
        % (STARTBLOCK_TOKEN, ENDBLOCK_TOKEN),
        r"(?P<inlinecode2>!?%s(?P<inline2>.*?)%s)" \
        % (INLINE_TOKEN, INLINE_TOKEN)]

    _post_rules = [
        r"(?P<citation>^(?P<cdepth>>(?: *>)*))",
        r"(?P<htmlescape>[&<>])",
        # shref corresponds to short TracLinks, i.e. sns:stgt
        r"(?P<shref>!?((?P<sns>%s):(?P<stgt>%s|%s(?:%s*%s)?)))" \
        % (LINK_SCHEME, QUOTED_STRING,
           SHREF_TARGET_FIRST, SHREF_TARGET_MIDDLE, SHREF_TARGET_LAST),
        # lhref corresponds to long TracLinks, i.e. [lns:ltgt label?]
        r"(?P<lhref>!?\[(?:(?P<lns>%s):(?P<ltgt>%s|[^\]\s]*)|(?P<rel>%s))"
        r"(?:\s+(?P<label>%s|[^\]]+))?\])" \
        % (LINK_SCHEME, QUOTED_STRING, LHREF_RELATIVE_TARGET, QUOTED_STRING),
        # macro call
        (r"(?P<macro>!?\[\[(?P<macroname>[\w/+-]+)"
         r"(\]\]|\((?P<macroargs>.*?)\)\]\]))"),
        # heading, list, definition, indent, table...
        r"(?P<heading>^\s*(?P<hdepth>=+)\s.*\s(?P=hdepth)\s*$)",
        r"(?P<list>^(?P<ldepth>\s+)(?:[-*]|\d+\.|[a-zA-Z]\.|[ivxIVX]{1,5}\.) )",
        r"(?P<definition>^\s+((?:%s.*?%s|%s.*?%s|[^%s%s])+?::)(?:\s+|$))"
        % (INLINE_TOKEN, INLINE_TOKEN, STARTBLOCK_TOKEN, ENDBLOCK_TOKEN,
           INLINE_TOKEN, STARTBLOCK[0]),
        r"(?P<indent>^(?P<idepth>\s+)(?=\S))",
        r"(?P<last_table_cell>\|\|\s*$)",
        r"(?P<table_cell>\|\|)"]

    _processor_re = re.compile('#\!([\w+-][\w+-/]*)')
    _anchor_re = re.compile('[^\w\d\.-:]+', re.UNICODE)

    # TODO: the following should be removed in milestone:0.11
    img_re = re.compile(r"\.(gif|jpg|jpeg|png)(\?.*)?$", re.IGNORECASE)

    def __init__(self, env, req=None, absurls=False, db=None):
        self.env = env
        self.req = req
        self._db = db
        self._absurls = absurls
        self._anchors = []
        self._open_tags = []
        self.href = absurls and env.abs_href or env.href
        self._local = env.config.get('project', 'url') or env.abs_href.base
        self.wiki = WikiSystem(self.env)

    def _get_db(self):
        if not self._db:
            self._db = self.env.get_db_cnx()
        return self._db
    db = property(fget=_get_db)

    # -- Rules preceeding IWikiSyntaxProvider rules: Font styles
    
    def tag_open_p(self, tag):
        """Do we currently have any open tag with @tag as end-tag"""
        return tag in self._open_tags

    def close_tag(self, tag):
        tmp =  ''
        for i in xrange(len(self._open_tags)-1, -1, -1):
            tmp += self._open_tags[i][1]
            if self._open_tags[i][1] == tag:
                del self._open_tags[i]
                for j in xrange(i, len(self._open_tags)):
                    tmp += self._open_tags[j][0]
                break
        return tmp

    def open_tag(self, open, close):
        self._open_tags.append((open, close))

    def simple_tag_handler(self, open_tag, close_tag):
        """Generic handler for simple binary style tags"""
        if self.tag_open_p((open_tag, close_tag)):
            return self.close_tag(close_tag)
        else:
            self.open_tag(open_tag, close_tag)
        return open_tag

    def _bolditalic_formatter(self, match, fullmatch):
        italic = ('<i>', '</i>')
        italic_open = self.tag_open_p(italic)
        tmp = ''
        if italic_open:
            tmp += italic[1]
            self.close_tag(italic[1])
        tmp += self._bold_formatter(match, fullmatch)
        if not italic_open:
            tmp += italic[0]
            self.open_tag(*italic)
        return tmp

    def _bold_formatter(self, match, fullmatch):
        return self.simple_tag_handler('<strong>', '</strong>')

    def _italic_formatter(self, match, fullmatch):
        return self.simple_tag_handler('<i>', '</i>')

    def _underline_formatter(self, match, fullmatch):
        if match[0] == '!':
            return match[1:]
        else:
            return self.simple_tag_handler('<span class="underline">',
                                           '</span>')

    def _strike_formatter(self, match, fullmatch):
        if match[0] == '!':
            return match[1:]
        else:
            return self.simple_tag_handler('<del>', '</del>')

    def _subscript_formatter(self, match, fullmatch):
        if match[0] == '!':
            return match[1:]
        else:
            return self.simple_tag_handler('<sub>', '</sub>')

    def _superscript_formatter(self, match, fullmatch):
        if match[0] == '!':
            return match[1:]
        else:
            return self.simple_tag_handler('<sup>', '</sup>')

    def _inlinecode_formatter(self, match, fullmatch):
        return '<tt>%s</tt>' % escape(fullmatch.group('inline'))

    def _inlinecode2_formatter(self, match, fullmatch):
        return '<tt>%s</tt>' % escape(fullmatch.group('inline2'))

    # -- Rules following IWikiSyntaxProvider rules

    # HTML escape of &, < and >

    def _htmlescape_formatter(self, match, fullmatch):
        return match == "&" and "&amp;" or match == "<" and "&lt;" or "&gt;"

    # Short form (shref) and long form (lhref) of TracLinks

    def _unquote(self, text):
        if text and text[0] in "'\"" and text[0] == text[-1]:
            return text[1:-1]
        else:
            return text

    def _shref_formatter(self, match, fullmatch):
        ns = fullmatch.group('sns')
        target = self._unquote(fullmatch.group('stgt'))
        return self._make_link(ns, target, match, match)

    def _lhref_formatter(self, match, fullmatch):
        ns = fullmatch.group('lns')
        target = self._unquote(fullmatch.group('ltgt'))
        label = fullmatch.group('label')
        if not label: # e.g. `[http://target]` or `[wiki:target]`
            if target:
                if target.startswith('//'): # for `[http://target]`
                    label = ns+':'+target   # use `http://target`
                else:                       # for `wiki:target`
                    label = target          # use only `target`
            else: # e.g. `[search:]` 
                label = ns
        label = self._unquote(label)
        rel = fullmatch.group('rel')
        if rel:
            return self._make_relative_link(rel, label or rel)
        else:
            return self._make_link(ns, target, match, label)

    def _make_link(self, ns, target, match, label):
        # check first for an alias defined in trac.ini
        ns = self.env.config.get('intertrac', ns.upper()) or ns
        if ns in self.wiki.link_resolvers:
            return to_unicode(self.wiki.link_resolvers[ns](
                self, ns, target, escape(label, False)))
        elif target.startswith('//') or ns == "mailto":
            return self._make_ext_link(ns+':'+target, label)
        else:
            return self._make_intertrac_link(ns, target, label) or \
                   self._make_interwiki_link(ns, target, label) or \
                   match

    def _make_intertrac_link(self, ns, target, label):
        url = self.env.config.get('intertrac', ns.upper() + '.url')
        if url:
            name = self.env.config.get('intertrac', ns.upper() + '.title',
                                       'Trac project %s' % ns)
            sep = target.find(':')
            if sep != -1:
                url = '%s/%s/%s' % (url, target[:sep], target[sep + 1:])
            else: 
                url = '%s/search?q=%s' % (url, urllib.quote_plus(target))
            return self._make_ext_link(url, label, '%s in %s' % (target, name))
        else:
            return None

    def shorthand_intertrac_helper(self, ns, target, label, fullmatch):
        if fullmatch: # short form
            it_group = fullmatch.group('it_%s' % ns)
            if it_group:
                alias = it_group.strip()
                intertrac = self.env.config.get('intertrac', alias.upper()) or \
                            alias
                target = '%s:%s' % (ns, target[len(it_group):])
                return self._make_intertrac_link(intertrac, target, label) or \
                       label
        return None

    def _make_interwiki_link(self, ns, target, label):
        interwiki = InterWikiMap(self.env)
        if interwiki.has_key(ns):
            url, title = interwiki.url(ns, target)
            return self._make_ext_link(url, label, title)
        else:
            return None

    def _make_ext_link(self, url, text, title=''):
        # ---- TODO: the following should be removed in milestone:0.11
        if Formatter.img_re.search(url) and self.flavor != 'oneliner':
            link = html.IMG(src=url, alt=title or text,
                            title='Warning: direct image links are deprecated,'
                            ' use [[Image(...)]] instead')
        # ----
        elif not url.startswith(self._local):
            link = html.A(html.SPAN(text, class_="icon"),
                          class_="ext-link", href=url, title=title or None)
        else:
            link = html.A(text, href=url, title=title or None)
        return unicode(link)

    def _make_relative_link(self, url, text):
        # ---- TODO: the following should be removed in milestone:0.11
        if Formatter.img_re.search(url) and self.flavor != 'oneliner':
            link = html.IMG(src=url, alt=text, title='Warning: direct image '
                            'links are deprecated, use [[Image(...)]] instead')
        # ----
        elif url.startswith('//'): # only the protocol will be kept
            link = html.A(text, class_="ext-link", href=url)
        else:
            link = html.A(text, href=url)
        return unicode(link)

    # WikiMacros
    
    def _macro_formatter(self, match, fullmatch):
        name = fullmatch.group('macroname')
        if name in ['br', 'BR']:
            return '<br />'
        args = fullmatch.group('macroargs')
        try:
            macro = WikiProcessor(self.env, name)
            return macro.process(self.req, args, True)
        except Exception, e:
            self.env.log.error('Macro %s(%s) failed' % (name, args),
                               exc_info=True)
            return system_message('Error: Macro %s(%s) failed' \
                                  % (name, args), e)

    # Headings

    def _heading_formatter(self, match, fullmatch):
        match = match.strip()
        self.close_table()
        self.close_paragraph()
        self.close_indentation()
        self.close_list()
        self.close_def_list()

        depth = min(len(fullmatch.group('hdepth')), 5)
        heading = match[depth + 1:len(match) - depth - 1]

        text = wiki_to_oneliner(heading, self.env, self.db, self._absurls)
        sans_markup = re.sub(r'</?\w+(?: .*?)?>', '', text)

        anchor = self._anchor_re.sub('', sans_markup)
        if not anchor or not anchor[0].isalpha():
            # an ID must start with a letter in HTML
            anchor = 'a' + anchor
        i = 1
        anchor = anchor_base = anchor
        while anchor in self._anchors:
            anchor = anchor_base + str(i)
            i += 1
        self._anchors.append(anchor)
        self.out.write('<h%d id="%s">%s</h%d>' % (depth, anchor, text, depth))

    # Generic indentation (as defined by lists and quotes)

    def _set_tab(self, depth):
        """Append a new tab if needed and truncate tabs deeper than `depth`

        given:       -*-----*--*---*--
        setting:              *
        results in:  -*-----*-*-------
        """
        tabstops = []
        for ts in self._tabstops:
            if ts >= depth:
                break
            tabstops.append(ts)
        tabstops.append(depth)
        self._tabstops = tabstops

    # Lists
    
    def _list_formatter(self, match, fullmatch):
        ldepth = len(fullmatch.group('ldepth'))
        listid = match[ldepth]
        self.in_list_item = True
        class_ = start = None
        if listid in '-*':
            type_ = 'ul'
        else:
            type_ = 'ol'
            idx = '01iI'.find(listid)
            if idx >= 0:
                class_ = ('arabiczero', None, 'lowerroman', 'upperroman')[idx]
            elif listid.isdigit():
                start = match[ldepth:match.find('.')]
            elif listid.islower():
                class_ = 'loweralpha'
            elif listid.isupper():
                class_ = 'upperalpha'
        self._set_list_depth(ldepth, type_, class_, start)
        return ''
        
    def _get_list_depth(self):
        """Return the space offset associated to the deepest opened list."""
        return self._list_stack and self._list_stack[-1][1] or 0

    def _set_list_depth(self, depth, new_type, list_class, start):
        def open_list():
            self.close_table()
            self.close_paragraph()
            self.close_indentation() # FIXME: why not lists in quotes?
            self._list_stack.append((new_type, depth))
            self._set_tab(depth)
            class_attr = list_class and ' class="%s"' % list_class or ''
            start_attr = start and ' start="%s"' % start or ''
            self.out.write('<'+new_type+class_attr+start_attr+'><li>')
        def close_list(tp):
            self._list_stack.pop()
            self.out.write('</li></%s>' % tp)

        # depending on the indent/dedent, open or close lists
        if depth > self._get_list_depth():
            open_list()
        else:
            while self._list_stack:
                deepest_type, deepest_offset = self._list_stack[-1]
                if depth >= deepest_offset:
                    break
                close_list(deepest_type)
            if depth > 0:
                if self._list_stack:
                    old_type, old_offset = self._list_stack[-1]
                    if new_type and old_type != new_type:
                        close_list(old_type)
                        open_list()
                    else:
                        if old_offset != depth: # adjust last depth
                            self._list_stack[-1] = (old_type, depth)
                        self.out.write('</li><li>')
                else:
                    open_list()

    def close_list(self):
        self._set_list_depth(0, None, None, None)

    # Definition Lists

    def _definition_formatter(self, match, fullmatch):
        tmp = self.in_def_list and '</dd>' or '<dl>'
        definition = match[:match.find('::')]
        tmp += '<dt>%s</dt><dd>' % wiki_to_oneliner(definition, self.env,
                                                    self.db)
        self.in_def_list = True
        return tmp

    def close_def_list(self):
        if self.in_def_list:
            self.out.write('</dd></dl>\n')
        self.in_def_list = False

    # Blockquote

    def _indent_formatter(self, match, fullmatch):
        idepth = len(fullmatch.group('idepth'))
        if self._list_stack:
            ltype, ldepth = self._list_stack[-1]
            if idepth < ldepth:
                for _, ldepth in self._list_stack:
                    if idepth > ldepth:
                        self.in_list_item = True
                        self._set_list_depth(idepth, None, None, None)
                        return ''
            elif idepth <= ldepth + (ltype == 'ol' and 3 or 2):
                self.in_list_item = True
                return ''
        if not self.in_def_list:
            self._set_quote_depth(idepth)
        return ''

    def _citation_formatter(self, match, fullmatch):
        cdepth = len(fullmatch.group('cdepth').replace(' ', ''))
        self._set_quote_depth(cdepth, True)
        return ''

    def close_indentation(self):
        self._set_quote_depth(0)

    def _get_quote_depth(self):
        """Return the space offset associated to the deepest opened quote."""
        return self._quote_stack and self._quote_stack[-1] or 0

    def _set_quote_depth(self, depth, citation=False):
        def open_quote(depth):
            self.close_table()
            self.close_paragraph()
            self.close_list()
            def open_one_quote(d):
                self._quote_stack.append(d)
                self._set_tab(d)
                class_attr = citation and ' class="citation"' or ''
                self.out.write('<blockquote%s>' % class_attr + os.linesep)
            if citation:
                for d in range(quote_depth+1, depth+1):
                    open_one_quote(d)
            else:
                open_one_quote(depth)
        def close_quote():
            self.close_table()
            self.close_paragraph()
            self._quote_stack.pop()
            self.out.write('</blockquote>' + os.linesep)
        quote_depth = self._get_quote_depth()
        if depth > quote_depth:
            self._set_tab(depth)
            tabstops = self._tabstops[::-1]
            while tabstops:
                tab = tabstops.pop()
                if tab > quote_depth:
                    open_quote(tab)
        else:
            while self._quote_stack:
                deepest_offset = self._quote_stack[-1]
                if depth >= deepest_offset:
                    break
                close_quote()
            if not citation and depth > 0:
                if self._quote_stack:
                    old_offset = self._quote_stack[-1]
                    if old_offset != depth: # adjust last depth
                        self._quote_stack[-1] = depth
                else:
                    open_quote(depth)
        if depth > 0:
            self.in_quote = True

    # Table
    
    def _last_table_cell_formatter(self, match, fullmatch):
        return ''

    def _table_cell_formatter(self, match, fullmatch):
        self.open_table()
        self.open_table_row()
        if self.in_table_cell:
            return '</td><td>'
        else:
            self.in_table_cell = 1
            return '<td>'

    def open_table(self):
        if not self.in_table:
            self.close_paragraph()
            self.close_list()
            self.close_def_list()
            self.in_table = 1
            self.out.write('<table class="wiki">' + os.linesep)

    def open_table_row(self):
        if not self.in_table_row:
            self.open_table()
            self.in_table_row = 1
            self.out.write('<tr>')

    def close_table_row(self):
        if self.in_table_row:
            self.in_table_row = 0
            if self.in_table_cell:
                self.in_table_cell = 0
                self.out.write('</td>')

            self.out.write('</tr>')

    def close_table(self):
        if self.in_table:
            self.close_table_row()
            self.out.write('</table>' + os.linesep)
            self.in_table = 0

    # Paragraphs

    def open_paragraph(self):
        if not self.paragraph_open:
            self.out.write('<p>' + os.linesep)
            self.paragraph_open = 1

    def close_paragraph(self):
        if self.paragraph_open:
            while self._open_tags != []:
                self.out.write(self._open_tags.pop()[1])
            self.out.write('</p>' + os.linesep)
            self.paragraph_open = 0

    def replace(self, fullmatch):
        for itype, match in fullmatch.groupdict().items():
            if match and not itype in self.wiki.helper_patterns:
                # Check for preceding escape character '!'
                if match[0] == '!':
                    return match[1:]
                if itype in self.wiki.external_handlers:
                    return to_unicode(self.wiki.external_handlers[itype](
                        self, match, fullmatch))
                else:
                    return getattr(self, '_' + itype + '_formatter')(match,
                                                                     fullmatch)
    # Code blocks
    
    def handle_code_block(self, line):
        if line.strip() == Formatter.STARTBLOCK:
            self.in_code_block += 1
            if self.in_code_block == 1:
                self.code_processor = None
                self.code_text = ''
            else:
                self.code_text += line + os.linesep
                if not self.code_processor:
                    self.code_processor = WikiProcessor(self.env, 'default')
        elif line.strip() == Formatter.ENDBLOCK:
            self.in_code_block -= 1
            if self.in_code_block == 0 and self.code_processor:
                self.close_table()
                self.close_paragraph()
                self.out.write(self.code_processor.process(self.req, self.code_text))
            else:
                self.code_text += line + os.linesep
        elif not self.code_processor:
            match = Formatter._processor_re.search(line)
            if match:
                name = match.group(1)
                self.code_processor = WikiProcessor(self.env, name)
            else:
                self.code_text += line + os.linesep 
                self.code_processor = WikiProcessor(self.env, 'default')
        else:
            self.code_text += line + os.linesep

    def close_code_blocks(self):
        while self.in_code_block > 0:
            self.handle_code_block(Formatter.ENDBLOCK)

    # -- Wiki engine
    
    def format(self, text, out, escape_newlines=False):
        self.out = out
        self._open_tags = []
        self._list_stack = []
        self._quote_stack = []
        self._tabstops = []

        self.in_code_block = 0
        self.in_table = 0
        self.in_def_list = 0
        self.in_table_row = 0
        self.in_table_cell = 0
        self.paragraph_open = 0

        for line in text.splitlines():
            # Handle code block
            if self.in_code_block or line.strip() == Formatter.STARTBLOCK:
                self.handle_code_block(line)
                continue
            # Handle Horizontal ruler
            elif line[0:4] == '----':
                self.close_table()
                self.close_paragraph()
                self.close_indentation()
                self.close_list()
                self.close_def_list()
                self.out.write('<hr />' + os.linesep)
                continue
            # Handle new paragraph
            elif line == '':
                self.close_paragraph()
                self.close_indentation()
                self.close_list()
                self.close_def_list()
                continue

            # Tab expansion and clear tabstops if no indent
            line = line.replace('\t', ' '*8)
            if not line.startswith(' '):
                self._tabstops = []

            if escape_newlines:
                line += ' [[BR]]'
            self.in_list_item = False
            self.in_quote = False
            # Throw a bunch of regexps on the problem
            result = re.sub(self.wiki.rules, self.replace, line)

            if not self.in_list_item:
                self.close_list()

            if not self.in_quote:
                self.close_indentation()

            if self.in_def_list and not line.startswith(' '):
                self.close_def_list()

            if self.in_table and line.strip()[0:2] != '||':
                self.close_table()

            if len(result) and not self.in_list_item and not self.in_def_list \
                    and not self.in_table:
                self.open_paragraph()
            out.write(result + os.linesep)
            self.close_table_row()

        self.close_table()
        self.close_paragraph()
        self.close_indentation()
        self.close_list()
        self.close_def_list()
        self.close_code_blocks()


class OneLinerFormatter(Formatter):
    """
    A special version of the wiki formatter that only implement a
    subset of the wiki formatting functions. This version is useful
    for rendering short wiki-formatted messages on a single line
    """
    flavor = 'oneliner'

    def __init__(self, env, absurls=False, db=None):
        Formatter.__init__(self, env, None, absurls, db)

    # Override a few formatters to disable some wiki syntax in "oneliner"-mode
    def _list_formatter(self, match, fullmatch): return match
    def _indent_formatter(self, match, fullmatch): return match
    def _citation_formatter(self, match, fullmatch): return escape(match)
    def _heading_formatter(self, match, fullmatch):
        return escape(match, False)
    def _definition_formatter(self, match, fullmatch):
        return escape(match, False)
    def _table_cell_formatter(self, match, fullmatch): return match
    def _last_table_cell_formatter(self, match, fullmatch): return match

    def _macro_formatter(self, match, fullmatch):
        name = fullmatch.group('macroname')
        if name.lower() == 'br':
            return ' '
        elif name == 'comment':
            return ''
        else:
            args = fullmatch.group('macroargs')
            return '[[%s%s]]' % (name,  args and '(...)' or '')

    def format(self, text, out, shorten=False):
        if not text:
            return
        self.out = out
        self._open_tags = []

        # Simplify code blocks
        in_code_block = 0
        processor = None
        buf = StringIO()
        for line in text.strip().splitlines():
            if line.strip() == Formatter.STARTBLOCK:
                in_code_block += 1
            elif line.strip() == Formatter.ENDBLOCK:
                if in_code_block:
                    in_code_block -= 1
                    if in_code_block == 0:
                        if processor != 'comment':
                            print>>buf, ' ![...]'
                        processor = None
            elif in_code_block:
                if not processor:
                    if line.startswith('#!'):
                        processor = line[2:].strip()
            else:
                print>>buf, line
        result = buf.getvalue()[:-1]

        if shorten:
            result = shorten_line(result)

        result = re.sub(self.wiki.rules, self.replace, result)
        result = result.replace('[...]', '[&hellip;]')
        if result.endswith('...'):
            result = result[:-3] + '&hellip;'

        # Close all open 'one line'-tags
        result += self.close_tag(None)
        # Flush unterminated code blocks
        if in_code_block > 0:
            result += '[&hellip;]'
        out.write(result)


class OutlineFormatter(Formatter):
    """Special formatter that generates an outline of all the headings in wiki
    text."""
    flavor = 'outline'
    
    def __init__(self, env, absurls=False, db=None):
        Formatter.__init__(self, env, None, absurls, db)

    # Override a few formatters to disable some wiki syntax in "outline"-mode
    def _citation_formatter(self, match, fullmatch): return escape(match)
    def _macro_formatter(self, match, fullmatch): return match

    def handle_code_block(self, line):
        if line.strip() == Formatter.STARTBLOCK:
            self.in_code_block += 1
        elif line.strip() == Formatter.ENDBLOCK:
            self.in_code_block -= 1

    def format(self, text, out, max_depth=6, min_depth=1):
        self.outline = []
        class NullOut(object):
            def write(self, data): pass
        Formatter.format(self, text, NullOut())

        if min_depth > max_depth:
            min_depth, max_depth = max_depth, min_depth
        max_depth = min(6, max_depth)
        min_depth = max(1, min_depth)

        curr_depth = min_depth - 1
        for depth, link in self.outline:
            if depth < min_depth or depth > max_depth:
                continue
            if depth < curr_depth:
                out.write('</li></ol><li>' * (curr_depth - depth))
            elif depth > curr_depth:
                out.write('<ol><li>' * (depth - curr_depth))
            else:
                out.write("</li><li>\n")
            curr_depth = depth
            out.write(link)
        out.write('</li></ol>' * curr_depth)

    def _heading_formatter(self, match, fullmatch):
        Formatter._heading_formatter(self, match, fullmatch)
        depth = min(len(fullmatch.group('hdepth')), 5)
        heading = match[depth + 1:len(match) - depth - 1]
        anchor = self._anchors[-1]
        text = wiki_to_oneliner(heading, self.env, self.db, self._absurls)
        text = re.sub(r'</?a(?: .*?)?>', '', text) # Strip out link tags
        self.outline.append((depth, '<a href="#%s">%s</a>' % (anchor, text)))


def wiki_to_html(wikitext, env, req, db=None,
                 absurls=False, escape_newlines=False):
    if not wikitext:
        return ''
    out = StringIO()
    Formatter(env, req, absurls, db).format(wikitext, out, escape_newlines)
    return Markup(out.getvalue())

def wiki_to_oneliner(wikitext, env, db=None, shorten=False, absurls=False):
    if not wikitext:
        return ''
    out = StringIO()
    OneLinerFormatter(env, absurls, db).format(wikitext, out, shorten)
    return Markup(out.getvalue())

def wiki_to_outline(wikitext, env, db=None,
                    absurls=False, max_depth=None, min_depth=None):
    if not wikitext:
        return ''
    out = StringIO()
    OutlineFormatter(env, absurls, db).format(wikitext, out, max_depth,
                                              min_depth)
    return Markup(out.getvalue())


class InterWikiMap(Component):
    """Implements support for InterWiki maps."""

    implements(IWikiChangeListener, IWikiMacroProvider)

    _page_name = 'InterMapTxt'
    _interwiki_re = re.compile(r"(%s)[ \t]+([^ \t]+)(?:[ \t]+#(.*))?" %
                               Formatter.LINK_SCHEME, re.UNICODE)
    _argspec_re = re.compile(r"\$\d")

    def __init__(self):
        self._interwiki_map = None
        # This dictionary maps upper-cased namespaces
        # to (namespace, prefix, title) values

    def _expand(self, txt, args):
        def setarg(match):
            num = int(match.group()[1:])
            return 0 < num <= len(args) and args[num-1] or ''
        return re.sub(InterWikiMap._argspec_re, setarg, txt)

    def _expand_or_append(self, txt, args):
        if not args:
            return txt
        expanded = self._expand(txt, args)
        return expanded == txt and txt + args[0] or expanded

    def has_key(self, ns):
        if not self._interwiki_map:
            self._update()
        return self._interwiki_map.has_key(ns.upper())

    def url(self, ns, target):
        ns, url, title = self._interwiki_map[ns.upper()]
        args = target.split(':')
        expanded_url = self._expand_or_append(url, args)
        expanded_title = self._expand(title, args)
        if expanded_title == title:
            expanded_title = target+' in '+title
        return expanded_url, expanded_title

    # IWikiChangeListener methods

    def wiki_page_added(self, page):
        if page.name == InterWikiMap._page_name:
            self._update()

    def wiki_page_changed(self, page, version, t, comment, author, ipnr):
        if page.name == InterWikiMap._page_name:
            self._update()

    def wiki_page_deleted(self, page):
        if page.name == InterWikiMap._page_name:
            self._interwiki_map.clear()

    def wiki_page_version_deleted(self, page):
        if page.name == InterWikiMap._page_name:
            self._update()

    def _update(self):
        from trac.wiki.model import WikiPage
        self._interwiki_map = {}
        content = WikiPage(self.env, InterWikiMap._page_name).text
        in_map = False
        for line in content.split('\n'):
            if in_map:
                if line.startswith('----'):
                    in_map = False
                else:
                    m = re.match(InterWikiMap._interwiki_re, line)
                    if m:
                        prefix, url, title = m.groups()
                        url = url.strip()
                        title = title and title.strip() or prefix
                        self._interwiki_map[prefix.upper()] = (prefix, url,
                                                               title)
            elif line.startswith('----'):
                in_map = True

    # IWikiMacroProvider methods

    def get_macros(self):
        yield 'InterWiki'

    def get_macro_description(self, name): 
        return "Provide a description list for the known InterWiki prefixes."

    def render_macro(self, req, name, content):
        from trac.util import sorted
        from trac.util.markup import html as _
        if not self._interwiki_map:
            self._update()
            
        interwikis = []
        for k in sorted(self._interwiki_map.keys()):
            prefix, url, title = self._interwiki_map[k]
            interwikis.append({
                'prefix': prefix, 'url': url, 'title': title,
                'rc_url': self._expand_or_append(url, ['RecentChanges']),
                'description': title == prefix and url or title})

        return _.TABLE(_.TR(_.TH(_.EM("Prefix")), _.TH(_.EM("Site"))),
                       [ _.TR(_.TD(_.A(w['prefix'], href=w['rc_url'])),
                              _.TD(_.A(w['description'], href=w['url'])))
                         for w in interwikis ],
                       class_="wiki interwiki")
