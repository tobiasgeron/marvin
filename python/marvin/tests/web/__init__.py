# !usr/bin/env python2
# -*- coding: utf-8 -*-
#
# Licensed under a 3-clause BSD license.
#
# @Author: Brian Cherinka
# @Date:   2017-02-12 17:38:51
# @Last modified by:   Brian Cherinka
# @Last Modified time: 2017-04-06 13:20:02

from __future__ import print_function, division, absolute_import
from flask_testing import TestCase, LiveServerTestCase
from marvin.web import create_app
from marvin import config, marvindb
from marvin.tests import MarvinTest
from selenium import webdriver
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
import requests
import os


class MarvinFrontEnd(MarvinTest, LiveServerTestCase):

    def create_app(self):
        app = create_app(debug=True, local=True, use_profiler=False)
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        app.config['PRESERVE_CONTEXT_ON_EXCEPTION'] = False
        app.config['LIVESERVER_PORT'] = 8943
        app.config['LIVESERVER_TIMEOUT'] = 10
        return app

    @classmethod
    def setUpClass(cls):
        super(MarvinFrontEnd, cls).setUpClass()

    def setUp(self):
        browserstack = os.environ.get('USE_BROWSERSTACK', None)
        if browserstack:
            self.desired_cap = {'os': 'OS X', 'os_version': 'El Capitan', 'browser': 'chrome', 'browser_version': '55', 'project': 'marvin'}
            self.desired_cap['browserstack.local'] = True
            self.desired_cap['browserstack.localIdentifier'] = os.environ['BROWSERSTACK_LOCAL_IDENTIFIER']
            self.driver = webdriver.Remote(
                command_executor='http://briancherinka1:RzufnqxUTip24gDaG6P6@hub.browserstack.com:80/wd/hub',
                desired_capabilities=self.desired_cap)
        else:
            self.driver = webdriver.Chrome()

        self.url = '{0}/marvin2/'.format(self.get_server_url())
        self.driver.get(self.url)

    def tearDown(self):
        self.driver.quit()

    def test_server_is_up_and_running(self):
        response = requests.get(self.url)
        self.assertEqual(response.status_code, 200)


class MarvinWebTester(MarvinTest, TestCase):
    ''' Base Marvin Web Tester for Flask and API '''

    def create_app(self):
        app = create_app(debug=True, local=True, use_profiler=False)
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        app.config['PRESERVE_CONTEXT_ON_EXCEPTION'] = False
        return app

    @classmethod
    def setUpClass(cls):
        super(MarvinWebTester, cls).setUpClass()

    def setUp(self):
        marvindb = self._marvindb
        self.session = marvindb.session
        self.long_message = True
        self.response = None
        self.data = None
        self.json = None
        self.set_sasurl('local')
        config.forceDbOn()
        self.urlmap = config.urlmap
        self.blue = None

    def tearDown(self):
        pass

    def _load_page(self, reqtype, page, params=None):
        if reqtype == 'get':
            self.response = self.client.get(page, query_string=params)
        elif reqtype == 'post':
            self.response = self.client.post(page, data=params, content_type='application/x-www-form-urlencoded')
        self._load_data()

    def _load_data(self):
        try:
            self.json = self.response.json
        except ValueError as e:
            self.json = None
        self.data = self.json['data'] if self.json and 'data' in self.json else ''

    def get_url(self, endpoint):
        return self.urlmap[self.blue][endpoint]['url']

    def assert422(self, response, message=None):
        self.assertStatus(response, 422, message)

    def assertListIn(self, a, b):
        ''' assert all items in list a are in b '''
        for item in a:
            self.assertIn(item, b)

    def _assert_webjson_success(self, data):
        self.assert200(self.response, message='response status should be 200 for ok')
        if isinstance(data, str):
            self.assertIn(data, self.json['result'])
        elif isinstance(data, dict):
            self.assertEqual(1, self.json['result']['status'])
            self.assertDictContainsSubset(data, self.json['result'])
        elif isinstance(data, list):
            self.assertListIn(data, self.json['result'])

    def _route_no_valid_webparams(self, url, noparam, reqtype='get', params=None, errmsg=None):
        self._load_page(reqtype, url, params=params)
        self.assert422(self.response, message='response status should be 422 for invalid params')
        self.assert_template_used('errors/unprocessable_entity.html')
        noparam = [noparam] if not isinstance(noparam, list) else noparam
        invalid = {p: [errmsg] for p in noparam}
        self.assert_context('data', invalid, message='response should contain validation error dictionary')

    def test_db_stuff(self):
        self.assertIsNotNone(marvindb)
        self.assertIsNotNone(marvindb.datadb)
        self.assertIsNotNone(marvindb.sampledb)
        self.assertIsNotNone(marvindb.dapdb)
        self.assertEqual('local', marvindb.dbtype)

    # def assert_template_used(self, name):
    #     ''' overriding the built-in one in Flask-Testing so we can also test against error templates '''
    #     template_list = self.app.jinja_env.list_templates()
    #     if name in template_list:
    #         return True
    #     else:
    #         raise AssertionError("Template {0} not used. Templates were used: {1}".format(name, ', '.join(template_list)))

