# -*- coding: utf-8 -*-
#
# Copyright (C) 2005-2006 Edgewall Software
# Copyright (C) 2005-2006 Christopher Lenz <cmlenz@gmx.de>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at http://trac.edgewall.org/wiki/TracLicense.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at http://trac.edgewall.org/log/.
#
# Author: Christopher Lenz <cmlenz@gmx.de>

from datetime import datetime
import imp
import inspect
import os
import re
try:
    set
except NameError:
    from sets import Set as set
from StringIO import StringIO

from genshi.builder import tag

from trac.config import default_dir
from trac.core import *
from trac.util.datefmt import format_date, utc
from trac.util.compat import sorted, groupby, any
from trac.util.html import escape, html, Markup
from trac.wiki.api import IWikiMacroProvider, WikiSystem, parse_args
from trac.wiki.model import WikiPage
from trac.web.chrome import add_stylesheet


class WikiMacroBase(Component):
    """Abstract base class for wiki macros."""

    implements(IWikiMacroProvider)
    abstract = True

    def get_macros(self):
        """Yield the name of the macro based on the class name."""
        name = self.__class__.__name__
        if name.endswith('Macro'):
            name = name[:-5]
        yield name

    def get_macro_description(self, name):
        """Return the subclass's docstring."""
        return inspect.getdoc(self.__class__)

    def parse_macro(self, parser, name, content):
        raise NotImplementedError

    def expand_macro(self, formatter, name, content):
        # -- TODO: remove in 0.12
        if hasattr(self, 'render_macro'):
            self.log.warning('Executing pre-0.11 Wiki macro %s by provider %s'
                             % (name, self.__class__))            
            return self.render_macro(formatter.req, name, content)
        # -- 
        raise NotImplementedError


class TitleIndexMacro(WikiMacroBase):
    """Inserts an alphabetic list of all wiki pages into the output.

    Accepts a prefix string as parameter: if provided, only pages with names
    that start with the prefix are included in the resulting list. If this
    parameter is omitted, all pages are listed.

    Alternate `format` and `depth` can be specified:
     - `format=group`: The list of page will be structured in groups
       according to common prefix. This format also supports a `min=n`
       argument, where `n` is the minimal number of pages for a group.
     - `depth=n`: limit the depth of the pages to list. If set to 0,
       only toplevel pages will be shown, if set to 1, only immediate
       children pages will be shown, etc. If not set, or set to -1,
       all pages in the hierarchy will be shown.
    """

    SPLIT_RE = re.compile(r"( |/|[0-9])")

    def expand_macro(self, formatter, name, content):
        args, kw = parse_args(content)
        prefix = args and args[0] or None
        format = kw.get('format', '')
        minsize = max(int(kw.get('min', 2)), 2)
        depth = int(kw.get('depth', -1))
        start = prefix and prefix.count('/') or 0

        wiki = formatter.wiki
        pages = sorted(wiki.get_pages(prefix))

        if format != 'group':
            return tag.ul([tag.li(tag.a(wiki.format_page_name(page),
                                        href=formatter.href.wiki(page)))
                           for page in pages
                           if depth < 0 or depth >= page.count('/') - start])
        
        # Group by Wiki word and/or Wiki hierarchy
        pages = [(self.SPLIT_RE.split(wiki.format_page_name(page, split=True)),
                  page) for page in pages
                 if depth < 0 or depth >= page.count('/') - start]
        def split_in_groups(group):
            """Return list of pagename or (key, sublist) elements"""
            groups = []
            for key, subgrp in groupby(group, lambda (k,p): k and k[0] or ''):
                subgrp = [(k[1:],p) for k,p in subgrp]
                if key and len(subgrp) >= minsize:
                    sublist = split_in_groups(sorted(subgrp))
                    if len(sublist) == 1:
                        elt = (key+sublist[0][0], sublist[0][1])
                    else:
                        elt = (key, sublist)
                    groups.append(elt)
                else:
                    for elt in subgrp:
                        groups.append(elt[1])
            return groups

        def render_groups(groups):
            return tag.ul(
                [tag.li(isinstance(elt, tuple) and 
                        tag(tag.strong(elt[0]), render_groups(elt[1])) or
                        tag.a(wiki.format_page_name(elt),
                              href=formatter.href.wiki(elt)))
                 for elt in groups])
        return render_groups(split_in_groups(pages))
            

class RecentChangesMacro(WikiMacroBase):
    """Lists all pages that have recently been modified, grouping them by the
    day they were last modified.

    This macro accepts two parameters. The first is a prefix string: if
    provided, only pages with names that start with the prefix are included in
    the resulting list. If this parameter is omitted, all pages are listed.

    The second parameter is a number for limiting the number of pages returned.
    For example, specifying a limit of 5 will result in only the five most
    recently changed pages to be included in the list.
    """

    def expand_macro(self, formatter, name, content):
        prefix = limit = None
        if content:
            argv = [arg.strip() for arg in content.split(',')]
            if len(argv) > 0:
                prefix = argv[0]
                if len(argv) > 1:
                    limit = int(argv[1])

        cursor = formatter.db.cursor()

        sql = 'SELECT name, ' \
              '  max(version) AS max_version, ' \
              '  max(time) AS max_time ' \
              'FROM wiki'
        args = []
        if prefix:
            sql += ' WHERE name LIKE %s'
            args.append(prefix + '%')
        sql += ' GROUP BY name ORDER BY max_time DESC'
        if limit:
            sql += ' LIMIT %s'
            args.append(limit)
        cursor.execute(sql, args)

        entries_per_date = []
        prevdate = None
        for name, version, ts in cursor:
            time = datetime.fromtimestamp(ts, utc)
            date = format_date(time)
            if date != prevdate:
                prevdate = date
                entries_per_date.append((date, []))
            entries_per_date[-1][1].append((name, int(version)))

        return html.DIV(
            [html.H3(date) +
             html.UL([html.LI(
            html.A(formatter.wiki.format_page_name(name),
                   href=formatter.href.wiki(name)),
            ' ',
            version > 1 and 
            html.SMALL('(',
                       html.A('diff',
                              href=formatter.href.wiki(name, action='diff',
                                                       version=version)),
                       ')') \
            or None)
                      for name, version in entries])
             for date, entries in entries_per_date])


class PageOutlineMacro(WikiMacroBase):
    """Displays a structural outline of the current wiki page, each item in the
    outline being a link to the corresponding heading.

    This macro accepts three optional parameters:
    
     * The first is a number or range that allows configuring the minimum and
       maximum level of headings that should be included in the outline. For
       example, specifying "1" here will result in only the top-level headings
       being included in the outline. Specifying "2-3" will make the outline
       include all headings of level 2 and 3, as a nested list. The default is
       to include all heading levels.
     * The second parameter can be used to specify a custom title (the default
       is no title).
     * The third parameter selects the style of the outline. This can be
       either `inline` or `pullout` (the latter being the default). The `inline`
       style renders the outline as normal part of the content, while `pullout`
       causes the outline to be rendered in a box that is by default floated to
       the right side of the other content.
    """

    def expand_macro(self, formatter, name, content):
        min_depth, max_depth = 1, 6
        title = None
        inline = 0
        if content:
            argv = [arg.strip() for arg in content.split(',')]
            if len(argv) > 0:
                depth = argv[0]
                if '-' in depth:
                    min_depth, max_depth = [int(d) for d in depth.split('-', 1)]
                else:
                    min_depth = max_depth = int(depth)
                if len(argv) > 1:
                    title = argv[1].strip()
                    if len(argv) > 2:
                        inline = argv[2].strip().lower() == 'inline'

        outline = formatter.context.wiki_to_outline(formatter.source,
                                                    max_depth=max_depth,
                                                    min_depth=min_depth)
        if title:
            outline = tag.h4(title) + outline
        if not inline:
            outline = tag.div(outline, class_="wiki-toc")
        return outline


class ImageMacro(WikiMacroBase):
    """Embed an image in wiki-formatted text.
    
    The first argument is the file specification. The file specification may
    reference attachments or files in three ways:
     * `module:id:file`, where module can be either '''wiki''' or '''ticket''',
       to refer to the attachment named ''file'' of the specified wiki page or
       ticket.
     * `id:file`: same as above, but id is either a ticket shorthand or a Wiki
       page name.
     * `file` to refer to a local attachment named 'file'. This only works from
       within that wiki page or a ticket.
    
    Also, the file specification may refer to repository files, using the
    `source:file` syntax (`source:file@rev` works also).
    
    The remaining arguments are optional and allow configuring the attributes
    and style of the rendered `<img>` element:
     * digits and unit are interpreted as the size (ex. 120, 25%)
       for the image
     * `right`, `left`, `top` or `bottom` are interpreted as the alignment for
       the image
     * `nolink` means without link to image source.
     * `key=value` style are interpreted as HTML attributes or CSS style
       indications for the image. Valid keys are:
        * align, border, width, height, alt, title, longdesc, class, id
          and usemap
        * `border` can only be a number
    
    Examples:
    {{{
        [[Image(photo.jpg)]]                           # simplest
        [[Image(photo.jpg, 120px)]]                    # with size
        [[Image(photo.jpg, right)]]                    # aligned by keyword
        [[Image(photo.jpg, nolink)]]                   # without link to source
        [[Image(photo.jpg, align=right)]]              # aligned by attribute
    }}}
    
    You can use image from other page, other ticket or other module.
    {{{
        [[Image(OtherPage:foo.bmp)]]    # if current module is wiki
        [[Image(base/sub:bar.bmp)]]     # from hierarchical wiki page
        [[Image(#3:baz.bmp)]]           # if in a ticket, point to #3
        [[Image(ticket:36:boo.jpg)]]
        [[Image(source:/images/bee.jpg)]] # straight from the repository!
        [[Image(htdocs:foo/bar.png)]]   # image file in project htdocs dir.
    }}}
    
    ''Adapted from the Image.py macro created by Shun-ichi Goto
    <gotoh@taiyo.co.jp>''
    """

    def expand_macro(self, formatter, name, content):
        # args will be null if the macro is called without parenthesis.
        if not content:
            return ''
        # parse arguments
        # we expect the 1st argument to be a filename (filespec)
        args = content.split(',')
        if len(args) == 0:
            raise Exception("No argument.")
        filespec = args[0]
        size_re = re.compile('[0-9]+%?$')
        attr_re = re.compile('(align|border|width|height|alt'
                             '|title|longdesc|class|id|usemap)=(.+)')
        quoted_re = re.compile("(?:[\"'])(.*)(?:[\"'])$")
        attr = {}
        style = {}
        nolink = False
        for arg in args[1:]:
            arg = arg.strip()
            if size_re.match(arg):
                # 'width' keyword
                attr['width'] = arg
                continue
            if arg == 'nolink':
                nolink = True
                continue
            if arg in ('left', 'right', 'top', 'bottom'):
                style['float'] = arg
                continue
            match = attr_re.match(arg)
            if match:
                key, val = match.groups()
                m = quoted_re.search(val) # unquote "..." and '...'
                if m:
                    val = m.group(1)
                if key == 'align':
                    style['float'] = val
                elif key == 'border':
                    style['border'] = ' %dpx solid' % int(val);
                else:
                    attr[str(key)] = val # will be used as a __call__ keyword

        # parse filespec argument to get module and id if contained.
        parts = filespec.split(':')
        url = None
        if len(parts) == 3:                 # module:id:attachment
            if parts[0] in ['wiki', 'ticket']:
                module, id, file = parts
            else:
                raise Exception("%s module can't have attachments" % parts[0])
        elif len(parts) == 2:
            from trac.versioncontrol.web_ui import BrowserModule
            try:
                browser_links = [link for link,_ in 
                                 BrowserModule(self.env).get_link_resolvers()]
            except Exception:
                browser_links = []
            if parts[0] in browser_links:   # source:path
                module, file = parts
                rev = None
                if '@' in file:
                    file, rev = file.split('@')
                url = formatter.href.browser(file, rev=rev)
                raw_url = formatter.href.browser(file, rev=rev, format='raw')
                desc = filespec
            else: # #ticket:attachment or WikiPage:attachment
                # FIXME: do something generic about shorthand forms...
                id, file = parts
                if id and id[0] == '#':
                    module = 'ticket'
                    id = id[1:]
                elif id == 'htdocs':
                    raw_url = url = formatter.href.chrome('site', file)
                    desc = os.path.basename(file)
                elif id in ('http', 'https', 'ftp'): # external URLs
                    raw_url = url = desc = id+':'+file
                else:
                    module = 'wiki'
        elif len(parts) == 1:               # attachment
            file = filespec
            module, id = formatter.context.realm, formatter.context.id
            if module not in ['wiki', 'ticket']: # FIXME: shouldn't be needed
                raise Exception('Cannot reference local attachment from here')
        else:
            raise Exception('No filespec given')
        if not url: # this is an attachment
            from trac.attachment import Attachment
            attachment = Attachment(self.env, module, id, file)
            url = attachment.href(formatter.req)
            raw_url = attachment.href(formatter.req, format='raw')
            desc = attachment.description
        for key in ['title', 'alt']:
            if desc and not attr.has_key(key):
                attr[key] = desc
        if style:
            attr['style'] = '; '.join(['%s:%s' % (k, escape(v))
                                       for k, v in style.iteritems()])
        result = html.IMG(src=raw_url, **attr)
        if not nolink:
            result = html.A(result, href=url, style='padding:0; border:none')
        return result


class MacroListMacro(WikiMacroBase):
    """Displays a list of all installed Wiki macros, including documentation if
    available.
    
    Optionally, the name of a specific macro can be provided as an argument. In
    that case, only the documentation for that macro will be rendered.
    
    Note that this macro will not be able to display the documentation of
    macros if the `PythonOptimize` option is enabled for mod_python!
    """

    def expand_macro(self, formatter, name, content):
        from trac.wiki.formatter import system_message

        wikimacros = formatter.context('wiki', 'WikiMacros')
        def get_macro_descr():
            for macro_provider in formatter.wiki.macro_providers:
                for macro_name in macro_provider.get_macros():
                    if content and macro_name != content:
                        continue
                    try:
                        descr = macro_provider.get_macro_description(macro_name)
                        descr = wikimacros.wiki_to_html(descr or '')
                    except Exception, e:
                        descr = Markup(system_message(
                            "Error: Can't get description for macro %s" \
                            % macro_name, e))
                    yield (macro_name, descr)

        return html.DL([(html.DT(html.CODE('[[',macro_name,']]'),
                                 id='%s-macro' % macro_name),
                         html.DD(description))
                        for macro_name, description in get_macro_descr()])


class TracIniMacro(WikiMacroBase):
    """Produce documentation for Trac configuration file.

    Typically, this will be used in the TracIni page.
    Optional arguments are a configuration section filter,
    and a configuration option name filter: only the configuration
    options whose section and name start with the filters are output.
    """

    def expand_macro(self, formatter, name, filter):
        from trac.config import Option
        filter = filter or ''

        sections = set([section for section, option in Option.registry.keys()
                        if section.startswith(filter)])

        tracini = formatter.context('wiki', 'TracIni')
        return html.DIV(class_='tracini')(
            [(html.H2('[%s]' % section, id='%s-section' % section),
              html.TABLE(class_='wiki')(
                  html.TBODY([html.TR(html.TD(html.TT(option.name)),
                                      html.TD(tracini.wiki_to_oneliner(option.\
                                                                    __doc__)))
                              for option in sorted(Option.registry.values(),
                                                   key=lambda o: o.name)
                              if option.section == section])))
             for section in sorted(sections)])



class TracGuideTocMacro(WikiMacroBase):
    """
    This macro shows a quick and dirty way to make a table-of-contents
    for a set of wiki pages.
    """

    TOC = [('TracGuide',                    'Index'),
           ('TracInstall',                  'Installation'),
           ('TracInterfaceCustomization',   'Customization'),
           ('TracPlugins',                  'Plugins'),
           ('TracUpgrade',                  'Upgrading'),
           ('TracIni',                      'Configuration'),
           ('TracAdmin',                    'Administration'),
           ('TracBackup',                   'Backup'),
           ('TracLogging',                  'Logging'),
           ('TracPermissions' ,             'Permissions'),
           ('TracWiki',                     'The Wiki'),
           ('WikiFormatting',               'Wiki Formatting'),
           ('TracTimeline',                 'Timeline'),
           ('TracBrowser',                  'Repository Browser'),
           ('TracRevisionLog',              'Revision Log'),
           ('TracChangeset',                'Changesets'),
           ('TracTickets',                  'Tickets'),
           ('TracRoadmap',                  'Roadmap'),
           ('TracQuery',                    'Ticket Queries'),
           ('TracReports',                  'Reports'),
           ('TracRss',                      'RSS Support'),
           ('TracNotification',             'Notification'),
          ]

    def expand_macro(self, formatter, name, args):
        curpage = formatter.context.id

        # Provision for multilingual TOC (e.g. TranslateRu/TracGuide ...)
        lang = ''
        idx = curpage.find('/')
        if idx > 0:
            lang = curpage[:idx+1]
            
        return tag.div(tag.h4('Table of Contents'),
                       tag.ul([tag.li(tag.a(title,
                                            href=formatter.href.wiki(lang+ref)),
                                      class_=(ref == curpage and "active"))
                               for ref, title in self.TOC]),
                       class_="wiki-toc")
