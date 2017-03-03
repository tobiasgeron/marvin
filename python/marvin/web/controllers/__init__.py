# !usr/bin/env python2
# -*- coding: utf-8 -*-
#
# Licensed under a 3-clause BSD license.
#
# @Author: Brian Cherinka
# @Date:   2016-12-08 14:24:58
# @Last modified by:   Brian Cherinka
# @Last Modified time: 2017-02-19 02:34:56

from __future__ import print_function, division, absolute_import
from flask_classy import FlaskView
from flask import request
from marvin.web.web_utils import parseSession


class BaseWebView(FlaskView):
    ''' This is the Base Web View for all pages '''

    def __init__(self, page):
        self.base = {}
        self.base['intro'] = 'Welcome to Marvin!'
        self.update_title(page)
        self._endpoint = self._release = None
        self._drpver = self._dapver = None

    def before_request(self, *args, **kwargs):
        ''' this runs before every single request '''
        self.base['error'] = None
        self._endpoint = request.endpoint
        self._drpver, self._dapver, self._release = parseSession()

    def after_request(self, name, response):
        ''' this runs after every single request '''

        return response

    def update_title(self, page):
        ''' Update the title and page '''
        self.base['title'] = page.title().split('-')[0] if 'main' in page \
            else page.title().replace('-', ' | ')
        self.base['page'] = page

    def reset_dict(self, mydict, exclude=None):
        ''' resets the page dictionary '''
        if exclude:
            exclude = exclude if isinstance(exclude, list) else [exclude]
        diffkeys = set(mydict) - set(self.base)
        for key, val in mydict.items():
            if key in diffkeys and (exclude and key not in exclude):
                mydict[key] = '' if isinstance(val, str) else None
