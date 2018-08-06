# !usr/bin/env python
# -*- coding: utf-8 -*-
#
# Licensed under a 3-clause BSD license.
#
# @Author: Brian Cherinka
# @Date:   2018-08-04 20:09:38
# @Last modified by:   Brian Cherinka
# @Last Modified time: 2018-08-05 22:11:15

from __future__ import print_function, division, absolute_import, unicode_literals

import re
import warnings
import datetime
from collections import Counter, defaultdict
from functools import wraps
from operator import eq, ge, gt, le, lt, ne

import numpy as np
import six
from marvin import config
from marvin.api.api import Interaction
from marvin.core.exceptions import MarvinError, MarvinUserWarning
from marvin.tools.results import Results

if config.db:
    from marvin import marvindb
    from marvin.utils.datamodel.query import datamodel
    from marvin.utils.datamodel.query.base import query_params
    from marvin.utils.general.structs import string_folding_wrapper
    from sqlalchemy import bindparam, func
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.orm import aliased
    from sqlalchemy.sql.expression import desc
    from sqlalchemy_boolean_search import (BooleanSearchException, parse_boolean_search)


__all__ = ['Query', 'doQuery']

opdict = {'<=': le, '>=': ge, '>': gt, '<': lt, '!=': ne, '=': eq, '==': eq}


def doQuery(**kwargs):
    """Convenience function for building a Query and retrieving the Results.

    Parameters:
        N/A:
            See the :class:`~marvin.tools.query.Query` class for a list
            of inputs.

    Returns:
        query, results:
            A tuple containing the built
            :class:`~marvin.tools.query.Query` instance, and the
            :class:`~marvin.tools.results.Results` instance.
    """
    start = kwargs.pop('start', None)
    end = kwargs.pop('end', None)
    query_type = kwargs.pop('query_type', None)
    q = Query(**kwargs)
    try:
        res = q.run(start=start, end=end, query_type=query_type)
    except TypeError as e:
        warnings.warn('Cannot run, query object is None: {0}.'.format(e), MarvinUserWarning)
        res = None

    return q, res


def update_config(f):
    """Decorator that updates query object with new config drpver and dapver versions."""

    @wraps(f)
    def wrapper(self, *args, **kwargs):
        if self.query and self.data_origin == 'db':
            self.query = self.query.params({'drpver': self._drpver, 'dapver': self._dapver})
        return f(self, *args, **kwargs)
    return wrapper


def tree():
    return defaultdict(tree)


class Query(object):
    ''' A class to perform queries on the MaNGA dataset.

    This class is the main way of performing a query.  A query works by minimally
    specifying a string filter a string filter condition in a natural language SQL format,
    as well as, a list of desired parameters to return.

    Query will use a local database if it finds on.  Otherwise a remote query uses
    the API to run a query on the Utah Server and return the results.

    The Query returns a list of tupled parameters and passed them into the
    Marvin Results object.  The parameters are a combination of user-defined
    return parameters, parameters used in the filter condition, and a set of pre-defined
    default parameters.  The object plateifu or mangaid is always returned by default.
    For queries involving DAP properties, the bintype, template, and spaxel x and y are
    also returned by default.

    Parameters:
        search_filter (str):
            A (natural language) string containing the filter conditions
            in the query.
        return_params (list):
            A list of string parameter names desired to be returned in the query
        return_type (str):
            The requested Marvin Tool object that the results are converted into
        mode ({'local', 'remote', 'auto'}):
            The load mode to use. See :doc:`Mode secision tree</mode_decision>`.
        return_all (bool):
            If True, attempts to return the entire set of results. Default is False.
        default_params (list):
            Optionally specify additional parameters as defaults
        sort (str):
            The parameter name to sort the query on
        order ({'asc', 'desc'}):
            The sort order.  Can be either ascending or descending.
        limit (int):
            The number limit on the number of returned results
        count_threshold (int):
            The threshold number to begin paginating results.  Default is 1000.
        nexus (str):
            The name of the database table to use as the nexus point for building
            the join table tree.  Can only be set in local mode.
        caching (bool):
            If True, turns on the dogpile memcache caching of results. Default is True.
        verbose (bool):
            If True, turns on verbosity.

    '''

    def __init__(self, search_filter=None, return_params=None, return_type=None, mode=None,
                 return_all=False, default_params=None, nexus='cube', sort='mangaid', order='asc',
                 caching=True, limit=100, count_threshold=1000, verbose=False, **kwargs):

        # basic parameters
        self.release = kwargs.pop('release', config.release)
        self._drpver, self._dapver = config.lookUpVersions(release=self.release)
        self.mode = mode if mode is not None else config.mode
        self.data_origin = None

        # main parameters
        self.search_filter = search_filter
        self.return_params = return_params
        self.return_type = return_type
        self.default_params = default_params
        self.filter_params = {}
        self.params = []
        self._nexus = nexus
        self._results = None

        # optional parameters
        self.return_all = return_all
        self.sort = sort
        self.order = order
        self._caching = caching
        self.count_threshold = count_threshold
        self.limit = limit
        self.verbose = verbose

        # add db specific parameters
        if config.db:
            self.datamodel = datamodel[self.release]
            self._marvinform = self.datamodel._marvinform
            self.session = marvindb.session
            self._modelgraph = marvindb.modelgraph

        # timings
        self._run_time = None
        self._final_time = None

        # define the Query MMA
        self._set_mma()

        # initialize a query
        if self.data_origin == 'file':
            raise MarvinError('Cannot currently query a file')
        elif self.data_origin == 'db':
            self._init_local_query()
        elif self.data_origin == 'api':
            self._init_remote_query()

    def __repr__(self):
        return ('Marvin Query(filter={0}, mode={1}, data_origin={2})'
                .format(repr(self.search_filter), repr(self.mode), repr(self.data_origin)))

    @property
    def nexus(self):
        return self._nexus

    @nexus.setter
    def nexus(self, value):
        if not self.data_origin == 'db':
            raise MarvinError('Can only set nexus point with a local db origin')
        self._nexus = value

    def _set_mma(self):
        ''' Sets up the Query MMA system '''

        if self.mode == 'local':
            self._do_local()
        if self.mode == 'remote':
            self._do_remote()
        if self.mode == 'auto':
            try:
                self._do_local()
            except MarvinError as e:
                warnings.warn('local mode failed. Trying remote now.', MarvinUserWarning)
                self._do_remote()

        # Sanity check to make sure data_origin has been properly set.
        assert self.data_origin in ['file', 'db', 'api'], 'data_origin is not properly set.'

    def _do_local(self):
        ''' Sets up to perform queries locally. '''

        if not config.db:
            warnings.warn('No local database found. Cannot perform queries.', MarvinUserWarning)
            raise MarvinError('No local database found.  Query cannot be run in local mode')
        else:
            self.mode = 'local'
            self.data_origin = 'db'

    def _do_remote(self):
        ''' Sets up to perform queries remotely. '''

        if not config.urlmap:
            raise MarvinError('No URL Map found.  Cannot make remote query calls!')
        else:
            self.mode = 'remote'
            self.data_origin = 'api'

    def _init_local_query(self):
        ''' Initialize a local database query '''

        # set default parameters
        self._set_defaultparams()

        # get user-defined input parameters
        self._set_returnparams()

        # setup the search filter
        self._set_filter()

        # build the query
        self._build_query()

    def _init_remote_query(self):
        ''' Initialize a remote API query '''

        # set up the parameters
        returns = ','.join(self.return_params) if self.return_params else None
        self._remote_params = {'searchfilter': self.search_filter,
                               'params': returns,
                               'returntype': self.return_type,
                               'release': self.release,
                               'limit': self.limit,
                               'return_all': self.return_all,
                               'caching': self._caching}

    def run(self, start=None, end=None, query_type=None):
        ''' Runs a Query

        Runs a query either locally or remotely.

        Parameters:
            start (int):
                A starting index when slicing the query
            end (int):
                An ending index when slicing the query
            query_type (str):
                The type of SQLAlchemy to submit. Can be "raw", "core", "orm"

        Returns:
            An instance of the :class:`~marvin.tools.query.results.Results`
            class containing the results of your Query.

        Example:
            >>> # filter of "NSA redshift less than 0.1 and stellar mass > 1.e10"
            >>> searchfilter = 'nsa.z < 0.1 and nsa.elpetro_mass > 1.e10'
            >>> returnparams = ['cube.ra', 'cube.dec']
            >>> q = Query(search_filter=searchfilter, return_params=returnparams)
            >>> results = q.run()

        '''

        if self.data_origin == 'api':
            results = self._run_remote(start=start, end=end, query_type=query_type)
        elif self.data_origin == 'db':
            results = self._run_local(start=start, end=end, query_type=query_type)

        return results

    def _run_remote(self, start=None, end=None, query_type=None):
        ''' Run a remote Query

        Runs a query remotely.  Creates a dictionary of all input parameters and
        performs the appropriate API call.  On return, converts the JSON results
        into a Marvin Results object.

        Parameters:
            start (int):
                A starting index when slicing the query
            end (int):
                An ending index when slicing the query
            query_type (str):
                The type of SQLAlchemy to submit. Can be "raw", "core", "orm"

        Returns:
            An instance of the :class:`~marvin.tools.query.results.Results`
            class containing the results of your Query.

        '''

        if self.return_all:
            warnings.warn('Warning: Attempting to return all results. This may take a while or crash.')

        # Get the query route
        url = config.urlmap['api']['querycubes']['url']

        # Update the remote params
        self._remote_params.update({'start': start, 'end': end, 'query_type': query_type})

        # set the start time of query
        starttime = datetime.datetime.now()

        # Request the query
        try:
            ii = Interaction(route=url, params=self._remote_params, stream=True, datastream=self.return_all)
        except Exception as e:
            raise MarvinError('API Query call failed: {0}'.format(e))
        else:
            results = ii.getData()
            # retrive and set some parameters
            self._query_params_order = ii.results['queryparams_order']
            self.params = ii.results['params']
            self.query = ii.results['query']
            count = ii.results['count']
            chunk = int(ii.results['chunk'])
            totalcount = ii.results['totalcount']
            query_runtime = ii.results['runtime']
            resp_runtime = ii.response_time

        # do results stuff here
        if self.return_all:
            msg = 'Returning all {0} results'.format(totalcount)
        else:
            msg = 'Only returning the first {0} results.'.format(count)

        if self.verbose:
            print('Results contain of a total of {0}. {1}'.format(totalcount, msg))

        # get Marvin Results
        final = Results(results=results, query=self.query, mode=self.mode, queryobj=self, count=count,
                        returntype=self.return_type, totalcount=totalcount, chunk=chunk,
                        runtime=query_runtime, response_time=resp_runtime, start=start, end=end)

        # get the final time
        posttime = datetime.datetime.now()
        self._final_time = (posttime - starttime)

        return final

    def _run_local(self, start=None, end=None, query_type=None):
        ''' Run a local database Query

        Parameters:
            start (int):
                A starting index when slicing the query
            end (int):
                An ending index when slicing the query
            query_type (str):
                The type of SQLAlchemy to submit. Can be "raw", "core", "orm"

        Returns:
            An instance of the :class:`~marvin.tools.query.results.Results`
            class containing the results of your Query.

        '''

        # Check for adding a sort
        self._sort_query()

        # Check to add the cache
        if self._caching:
            from marvin.core.caching_query import FromCache
            self.query = self.query.options(FromCache("default")).\
                options(*marvindb.cache_bits)

        # turn on streaming of results
        self.query = self.query.execution_options(stream_results=True)

        # set the start time of query
        starttime = datetime.datetime.now()

        # check for query and get count
        totalcount = self._get_query_count()

        # slice the query
        query = self._slice_query(start=start, end=end, totalcount=totalcount)

        # run the query and get the results
        results = self._get_results(query, query_type=query_type, totalcount=totalcount)

        # get the runtime
        endtime = datetime.datetime.now()
        self._run_time = (endtime - starttime)

        # clear the session
        self.session.close()

        # convert to Marvin Results
        final = Results(results=results, query=query, count=self._count, mode=self.mode,
                        returntype=self.return_type, queryobj=self, totalcount=totalcount,
                        chunk=self.limit, runtime=self._run_time, start=self._start, end=self._end)

        # get the final time
        posttime = datetime.datetime.now()
        self._final_time = (posttime - starttime)

        return final

    def _sort_query(self):
        ''' Sort the SQLA query object by a given parameter '''

        if not isinstance(self.sort, type(None)):
            # set the sort variable ModelClass parameter
            if '.' in self.sort:
                param = self.datamodel.parameters[str(self.sort)].full
            else:
                param = self.datamodel.parameters.get_full_from_remote(self.sort)
            sortparam = self._marvinform._param_form_lookup.mapToColumn(param)

            # If order is specified, then do the sort
            if self.order:
                assert self.order in ['asc', 'desc'], 'Sort order parameter must be either "asc" or "desc"'

                # Check if order by already applied
                if 'ORDER' in str(self.query.statement):
                    self.query = self.query.order_by(None)
                # Do the sorting
                if 'desc' in self.order:
                    self.query = self.query.order_by(desc(sortparam))
                else:
                    self.query = self.query.order_by(sortparam)

    def _get_query_count(self):
        ''' Get the SQL query count of rows

        First checks the query history table to look up if this query has
        already been run and a count produced.

        Returns:
            The total count of rows for the query

        '''

        totalcount = None
        if marvindb.isdbconnected:
            qm = self._check_history(check_only=True)
            totalcount = qm.count if qm else None

        # run count if it doesn't exist
        if totalcount is None:
            totalcount = self.query.count()

        return totalcount

    def _check_history(self, check_only=None, totalcount=None):
        ''' Check the query against the query history schema

        Looks up the current query in the query table of
        the history schema and if found, returns the SQLA object

        Parameters:
            check_only (bool):
                If True, only checks the history schema but does not write to it
            totalcount (int):
                The total count of rows to add, when adding a new query to the table

        Returns:
            The SQLAlchemy row from the query table of the history schema

        '''

        sqlcol = self._marvinform._param_form_lookup.mapToColumn('sql')
        stringfilter = self.search_filter.strip().replace(' ', '')
        rawsql = self.show().strip()
        returns = ','.join(self.return_params) if self.return_params else ''
        qm = self.session.query(sqlcol.class_).\
            filter(sqlcol == rawsql, sqlcol.class_.release == self.release).one_or_none()

        if check_only:
            return qm

        with self.session.begin():
            if not qm:
                qm = sqlcol.class_(searchfilter=stringfilter, n_run=1, release=self.release,
                                   count=totalcount, sql=rawsql, return_params=returns)
                self.session.add(qm)
            else:
                qm.n_run += 1

        return qm

    def _slice_query(self, start=None, end=None, totalcount=None):
        ''' Slice the SQLA query object

        Parameters:
            start (int):
                A starting index when slicing the query
            end (int):
                An ending index when slicing the query
            totalcount (int):
                The total count of rows of the query

        Returns:
            A new SQLA query object that has been sliced

        '''

        # get the new count if start and end exist
        if start and end:
            count = (end - start)
        else:
            count = totalcount

        # # run the query
        # res = self.query.slice(start, end).all()
        # count = len(res)
        # self.totalcount = count if not self.totalcount else self.totalcount

        # check history
        if marvindb.isdbconnected:
            __ = self._check_history(totalcount=totalcount)

        if count > self.count_threshold and self.return_all is False:
            # res = res[0:self.limit]
            start = 0
            end = self.limit
            count = (end - start)
            warnings.warn('Results contain more than {0} entries.  '
                          'Only returning first {1}'.format(self.count_threshold, self.limit), MarvinUserWarning)
        elif self.return_all is True:
            warnings.warn('Warning: Attempting to return all results. This may take a long time or crash.', MarvinUserWarning)
            start = None
            end = None
        elif start and end:
            warnings.warn('Getting subset of data {0} to {1}'.format(start, end), MarvinUserWarning)

        # slice the query
        query = self.query.slice(start, end)

        # set updated start, end, count, and total
        self._start = start
        self._end = end
        self._count = count
        self._total = totalcount

        return query

    def _get_results(self, query, query_type=None, totalcount=None):
        ''' Get the raw results of the query

        Runs the SQLAlchemy query.  query_type will determine how the query is run.
        "raw" means the query is run using the psycopg2 cursor object. "core" means
        the query is run using the SQLA connection object.  The "raw" and "core" methods
        will submit the raw sql string and retrieve the results in chunks using `fetchall`.
        orm" means the query is running using the SQLA query object.  This uses yield_per
        to generate the results in chunk.  It also folds similar strings together.

        Parameters:
            query (object):
                The current SQLA query object
            query_type (str):
                The type of SQLAlchemy to submit. Can be "raw", "core", "orm". Default is raw.
            totalcount (int):
                The total count of rows of the query

        Returns:
            A list of tupled results

        '''

        if query_type:
            assert query_type in ['raw', 'core', 'orm'], 'Query Type can only be raw, core, or orm.'
        else:
            query_type = 'raw'

        # run the query
        if query_type == 'raw':
            # use the db api cursor
            sql = str(self._get_sql(query))
            conn = marvindb.db.engine.raw_connection()
            cursor = conn.cursor('query_cursor')
            cursor.execute(sql)
            res = self._fetch_data(cursor)
            conn.close()
        elif query_type == 'core':
            # use the core connection
            sql = str(self._get_sql(query))
            with marvindb.db.engine.connect() as conn:
                results = conn.execution_options(stream_results=True).execute(sql)
                res = self._fetch_data(results)
        elif query_type == 'orm':
            # use the orm query
            yield_num = int(10**(np.floor(np.log10(totalcount))))
            results = string_folding_wrapper(query.yield_per(yield_num), keys=self.params)
            res = list(results)

        return res

    def _fetch_data(self, obj, n_rows=100000):
        ''' Fetch query results using fetchall or fetchmany

        Parameters:
            obj (object):
                SQLAlchemy connection object or Pyscopg2 cursor object
            n_rows (int):
                The number of rows to fetch at a time

        Returns:
            A list of results from a query

        '''

        res = []

        if not self.return_all:
            res = obj.fetchall()
        else:
            while True:
                rows = obj.fetchmany(n_rows)
                if rows:
                    res.extend(rows)
                else:
                    break
        return res

    @staticmethod
    def _get_sql(query):
        ''' Get the sql for a given query

        Parameters:
            query (object):
                An SQLAlchemy Query object

        Returns:
            A raw sql string
        '''

        return query.statement.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True})

    def show(self, prop='query'):
        ''' Prints into the console

        Displays the query to the console with parameter variables plugged in.
        Works only in local mode.  Input prop can be one of query, joins, or filter.

        Allowed Values for Prop:
            - query: displays the entire query (default if nothing specified)
            - joins: displays the tables that have been joined in the query
            - filter: displays only the filter used on the query

        Parameters:
            prop (str):
                The type of info to print.  Can be 'query', 'joins', or 'filter'.

        Returns:
            The SQL string

        '''

        assert prop in [None, 'query', 'joins', 'filter'], 'Input must be query, joins, or filter'

        if self.data_origin == 'db':
            if not prop or prop == 'query':
                sql = self._get_sql(self.query)
            elif prop == 'joins':
                sql = self._joins
            elif prop == 'filter':
                if hasattr(self.query, 'whereclause'):
                    sql = self.query.whereclause.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True})
                else:
                    sql = 'cannot extract filter from where clause'
            else:
                sql = self.__getattribute__(prop)

            return str(sql)
        elif self.data_origin == 'api':
            sql = self.search_filter
            return sql

    def get_available_params(self, paramdisplay='best'):
        ''' Retrieve the available parameters to query on

        Retrieves a list of the available query parameters. Can either
        retrieve a list of all the parameters or only the vetted parameters.

        Parameters:
            paramdisplay (str {all|best}):
                String indicating to grab either all or just the vetted parameters.
                Default is to only return 'best', i.e. vetted parameters

        Returns:
            A list of all of the available queryable parameters
        '''
        assert paramdisplay in ['all', 'best'], 'paramdisplay can only be either "all" or "best"!'

        if paramdisplay == 'all':
            qparams = self.datamodel.groups.list_params('full')
        elif paramdisplay == 'best':
            qparams = query_params
        return qparams

    #
    # This section describes the methods that run for local database queries
    #
    def _set_defaultparams(self):
        ''' Set the default parameters

        Loads any default parameters set.  Will also include appropriate
        defaults when specifying an object return type

        Default Parameters for Objects:
            - Cubes/RSS - plateifu/mangaid
            - Maps/ModelCube - plateifu/mangaid, bintype, template
            - Spaxel - plateifu/mangaid, x, y, bintype, template

        '''

        if self.return_type:
            assert self.return_type in ['cube', 'spaxel', 'maps', 'rss',
                                        'modelcube'], ('Query return_type must be either cube, spaxel, '
                                                       'maps, modelcube, rss')

        # set some initial defaults
        assert isinstance(self.default_params, (list, type(None))), 'default_params must be a list'
        defaults = self.default_params or ['cube.mangaid', 'cube.plateifu']

        if self.return_type == 'cube':
            defaults.extend(['cube.mangaid', 'cube.plateifu'])
        elif self.return_type == 'spaxel':
            pass
        elif self.return_type == 'modelcube':
            defaults.extend(['bintype.name', 'template.name'])
        elif self.return_type == 'rss':
            pass
        elif self.return_type == 'maps':
            defaults.extend(['bintype.name', 'template.name'])

        self.default_params = defaults

        # add the defaults to the main set of parameters
        self.params.extend(self.default_params)

    def _set_returnparams(self):
        ''' Set the return parameters '''

        # set the initial returns list
        returns = self.return_params or []
        returns = [returns] if not isinstance(returns, list) else returns

        # look up shortcut names for the return parameters
        full_returnparams = [self._marvinform._param_form_lookup._nameShortcuts[rp]
                             if rp in self._marvinform._param_form_lookup._nameShortcuts else rp
                             for rp in returns]

        self.return_params = full_returnparams

        # remove any return parameters that are also defaults
        use_only = [f for f in full_returnparams if f not in self.default_params]

        # add the return parameters to the main set of parameters
        self.params.extend(use_only)

    def _set_filter(self):
        ''' Set up the search filter '''

        # do nothing if nothing
        if not self.search_filter:
            return

        # check and parse the SQL string
        self._parse_sql_string()

    def _parse_sql_string(self):
        ''' Parse the SQL string '''

        # if params is a string, then parse and filter
        if not isinstance(self.search_filter, six.string_types):
            raise MarvinError('Input parameters must be a natural language string!')
        else:
            self._check_shortcuts_in_filter()
            try:
                parsed = parse_boolean_search(self.search_filter)
            except BooleanSearchException as e:
                raise MarvinError('Your boolean expression contained a syntax error: {0}'.format(e))

        # update the parameters dictionary
        self._check_parsed(parsed)
        self.filter_params.update(parsed.params)
        filterkeys = [key for key in parsed.uniqueparams if key not in self.params]
        self.params.extend(filterkeys)

    def _check_shortcuts_in_filter(self):
        ''' Check for shortcuts in string filter and replace them '''

        # table shortcuts
        # for key in self.marvinform._param_form_lookup._tableShortcuts.keys():
        #     #if key in strfilter:
        #     if re.search('{0}.[a-z]'.format(key), strfilter):
        #         strfilter = strfilter.replace(key, self.marvinform._param_form_lookup._tableShortcuts[key])

        # name shortcuts
        for key in self._marvinform._param_form_lookup._nameShortcuts.keys():
            if key in self.search_filter:
                param_form_lookup = self._marvinform._param_form_lookup
                self.search_filter = re.sub(r'\b{0}\b'.format(key),
                                            '{0}'.format(param_form_lookup._nameShortcuts[key]),
                                            self.search_filter)

    def _check_parsed(self, parsed):
        ''' Check the boolean parsed object

            check for function conditions vs normal.  This should be moved
            into SQLalchemy Boolean Search
        '''

        # Triggers for only one filter and it is a function condition
        if hasattr(parsed, 'fxn'):
            parsed.functions = [parsed]

        self._parsed = parsed

    def _check_for(self, parameters, schema=None, tables=None):
        ''' Check if a schema or test of tables names are in the provided parameters '''

        fparams = self._marvinform._param_form_lookup.mapToColumn(parameters)
        fparams = [fparams] if not isinstance(fparams, list) else fparams
        if schema:
            inschema = [schema in c.class_.__table__.schema for c in fparams]
            return True if any(inschema) else False
        if tables:
            tables = [tables] if not isinstance(tables, list) else tables
            intables = sum([[t in c.class_.__table__.name for c in fparams] for t in tables], [])
            return True if any(intables) else False

    def _build_query(self):
        ''' Build the query '''

        # create SQLA query parameters
        self._set_query_parameters()

        # create base SQLA object
        self._create_base_query()

        # join tables
        self._join_tables()

        # add condition
        self._add_condition()

        # add PipelineInfo
        self._add_pipeline()

        # check if the query filter is against the DAP
        if self._check_for(self.filter_params.keys(), schema='dapdb'):
            self._build_dap_query()
            self._check_dapall_query()

    def _set_query_parameters(self):
        ''' Creates a list of database ModelClasses from a list of parameter names '''

        # adjust the default parameters for any necessary DAP
        if self._check_for(self.params, tables=['spaxelprop', 'modelspaxel']):
            dapcols = ['spaxelprop.x', 'spaxelprop.y', 'bintype.name', 'template.name']
            self.default_params.extend(dapcols)
            self.params.extend(dapcols)

        self.params = [item for item in self.params if item in set(self.params)]
        queryparams = self._marvinform._param_form_lookup.mapToColumn(self.params)
        self._query_params = [item for item in queryparams if item in set(queryparams)]
        self._query_params_order = [q.key for q in self._query_params]

    def _create_base_query(self):
        ''' Create the base query session object.  Passes in a list of parameters defined in
            returnparams, filterparams, and defaultparams
        '''
        labeledqps = [qp.label(self.params[i]) for i, qp in enumerate(self._query_params)]
        self.query = self.session.query(*labeledqps)

    @update_config
    def _join_tables(self):
        ''' Build the join statement from the input parameters '''

        #from marvin import marvindb
        ifu = marvindb.datadb.IFUDesign

        self._joins = []

        # build list of SQLA models for the joins from the parameters
        self._modellist = []
        for param in self._query_params:
            # add the proper parameter Model
            if param.class_ not in self._modellist:
                self._modellist.append(param.class_)

            # if plateifu is a parameter, make sure we add the IFUDesign Model
            if 'plateifu' in str(param) and ifu not in self._modellist:
                self._modellist.append(ifu)

        # if there are no additional join tables, return
        if len(set(self._modellist)) == 1:
            return

        # Look up the nexus point.  If nexus is still none, takes the most common table from
        # the list of query parameters.  Default nexus is cube.
        nexus = self._marvinform.look_up_table(self.nexus)
        if not nexus:
            nexus = Counter(self._modellist).most_common(1)[0][0]

        # Gets the list of joins from ModelGraph. Uses Cube as nexus, so that
        # the order of the joins is the correct one.
        joinmodellist = self._modelgraph.getJoins(self._modellist, format_out='models', nexus=nexus)

        # Add the tables from the join list into the query
        for model in joinmodellist:
            name = '{0}.{1}'.format(model.__table__.schema, model.__tablename__)
            if not self._table_in_query(name):
                self._joins.append(model.__tablename__)
                if 'template' not in model.__tablename__:
                    self.query = self.query.join(model)
                else:
                    # assume template_kin only now, TODO deal with template_pop later
                    self.query = self.query.join(model, marvindb.dapdb.Structure.template_kin)

    def _table_in_query(self, name):
        ''' Checks if a given SQL table is already in the SQL query '''

        # do the check
        try:
            isin = name in str(self.query._from_obj[0])
        except IndexError as e:
            isin = False
        except AttributeError as e:
            if isinstance(self.query, six.string_types):
                isin = name in self.query
            else:
                isin = False
        return isin

    def _add_condition(self):
        ''' Loop over all input forms and add a filter condition based on the input parameter form data. '''

        # do nothing if nothing
        if not self.search_filter:
            return

        # validate the forms
        self._validate_forms()

        # build the actual filter
        self._build_filter()

        # add the filter to the query
        if not isinstance(self.filter, type(None)):
            self.query = self.query.filter(self.filter)

    def _validate_forms(self):
        ''' Validate all the data in the forms '''

        errors = []
        forms = self._set_forms()
        isgood = [form.validate() for form in forms.values()]
        if not all(isgood):
            inds = np.where(np.invert(isgood))[0]
            for index in inds:
                errors.append(list(forms.values())[index].errors)
            raise MarvinError('Parameters failed to validate: {0}'.format(errors))

    def _set_forms(self):
        ''' Set the appropriate WTForms in myforms and set the parameters '''

        forms = defaultdict(str)
        paramtree = tree()
        for key in self.filter_params.keys():
            forms[key] = self._marvinform.callInstance(self._marvinform._param_form_lookup[key], params=self.filter_params)
            paramtree[forms[key].Meta.model.__name__][key]
        return forms

    def _build_filter(self):
        ''' Builds a filter condition to load into sqlalchemy filter. '''
        try:
            self.filter = self._parsed.filter(self._modellist)
        except BooleanSearchException as e:
            raise MarvinError('Your boolean expression could not me mapped to model: {0}'.format(e))

    def _add_pipeline(self):
        ''' Adds the DRP and DAP Pipeline Info into the Query '''

        self._drp_alias = aliased(marvindb.datadb.PipelineInfo, name='drpalias')
        self._dap_alias = aliased(marvindb.datadb.PipelineInfo, name='dapalias')

        drppipe = self._get_pipe_info('drp')
        dappipe = self._get_pipe_info('dap')

        # Add DRP pipeline version
        if drppipe:
            self.query = self.query.join(self._drp_alias, marvindb.datadb.Cube.pipelineInfo).\
                filter(self._drp_alias.pk == drppipe.pk)

        # Add DAP pipeline version
        if dappipe:
            self.query = self.query.join(self._dap_alias, marvindb.dapdb.File.pipelineinfo).\
                filter(self._dap_alias.pk == dappipe.pk)

    def _get_pipe_info(self, pipename):
        ''' Retrieve the pipeline Info for a given pipeline version name '''

        assert pipename.lower() in ['drp', 'dap'], 'Pipeline Name must either be DRP or DAP'

        # bindparam values
        bindname = 'drpver' if pipename.lower() == 'drp' else 'dapver'
        bindvalue = self._drpver if pipename.lower() == 'drp' else self._dapver

        # class names
        if pipename.lower() == 'drp':
            inclasses = self._table_in_query('cube') or 'cube' in str(self.query.statement.compile())
        elif pipename.lower() == 'dap':
            inclasses = self._table_in_query('file') or 'file' in str(self.query.statement.compile())

        # set alias
        pipealias = self._drp_alias if pipename.lower() == 'drp' else self._dap_alias

        # get the pipeinfo
        if inclasses:
            pipeinfo = marvindb.session.query(pipealias).\
                join(marvindb.datadb.PipelineName, marvindb.datadb.PipelineVersion).\
                filter(marvindb.datadb.PipelineName.label == pipename.upper(),
                       marvindb.datadb.PipelineVersion.version == bindparam(bindname, bindvalue)).one()
        else:
            pipeinfo = None

        return pipeinfo

    def _group_by(self, params=None):
        ''' Group the query by a set of parameters

        Parameters:
            params (list):
                A list of string parameter names to group the query by

        Returns:
            A new SQLA Query object
        '''

        if not params:
            params = [d for d in self.default_params if 'spaxelprop' not in d]

        #newdefaults = self._marvinform._param_form_lookup.mapToColumn(params)
        newdefaults = [d for d in self._query_params if str(d).lower() in params]
        self.params = params
        newq = self.query.from_self(*newdefaults).group_by(*newdefaults)
        return newq

    def _check_query(self, name):
        ''' Check if string is inside the query statement '''

        qstate = str(self.query.statement.compile(compile_kwargs={'literal_binds': True}))
        return name in qstate

    def _update_params(self, param):
        ''' Update the input parameters '''
        param = {key: val.decode('UTF-8') if '*' not in val.decode('UTF-8') else
                 val.replace('*', '%').decode('UTF-8') for key, val in param.items()
                 if key in self.filter_params.keys()}
        self.filter_params.update(param)

    def _already_in_filter(self, names):
        ''' Checks if the parameter name already added into the filter '''

        infilter = None
        if names:
            if not isinstance(self.query, type(None)):
                if not isinstance(self.query.whereclause, type(None)):
                    wc = str(self.query.whereclause.compile(dialect=postgresql.dialect(),
                             compile_kwargs={'literal_binds': True}))
                    infilter = any([name in wc for name in names])

        return infilter

    #
    # Methods specific to DAP zonal queries
    #
    def _build_dap_query(self):
        ''' Builds a DAP zonal query '''

        # get the appropriate SpaxelProp ModelClass
        self._spaxelclass = self._marvinform._param_form_lookup['spaxelprop.file'].Meta.model

        # check for additional modifier criteria
        if self._parsed.functions:
            # loop over all functions
            for fxn in self._parsed.functions:
                # look up the function name in the marvinform dictionary
                try:
                    methodname = self._marvinform._param_fxn_lookup[fxn.fxnname]
                except KeyError as e:
                    raise MarvinError('Could not set function: {0}'.format(e))
                else:
                    # run the method
                    methodcall = self.__getattribute__(methodname)
                    methodcall(fxn)

    def _get_good_spaxels(self):
        ''' Subquery - Counts the number of good spaxels

        Counts the number of good spaxels with binid != -1
        Uses the spaxelprop.bindid_pk != 9999 since this is known and set.
        Removes need to join to the binid table

        Returns:
            bincount (subquery):
                An SQLalchemy subquery to be joined into the main query object
        '''

        spaxelname = self._spaxelclass.__name__
        bincount = self.session.query(self._spaxelclass.file_pk.label('binfile'),
                                      func.count(self._spaxelclass.pk).label('goodcount'))

        # optionally add the filter if the table is SpaxelProp
        if 'CleanSpaxelProp' not in spaxelname:
            bincount = bincount.filter(self._spaxelclass.binid != -1)

        # group the results by file_pk
        bincount = bincount.group_by(self._spaxelclass.file_pk).subquery('bingood', with_labels=True)

        return bincount

    def _get_count_of(self, expression):
        ''' Subquery - Counts spaxels satisfying an expression

        Counts the number of spaxels of a given
        parameter above a certain value.

        Parameters:
            expression (str):
                The filter expression to parse

        Returns:
            valcount (subquery):
                An SQLalchemy subquery to be joined into the main query object

        Example:
            >>> expression = 'spaxelprop.emline_gflux_ha_6564 >= 25'
        '''

        # parse the expression into name, operator, value
        param, ops, value = self._parse_expression(expression)
        # look up the InstrumentedAttribute, Operator, and convert Value
        attribute = self._marvinform._param_form_lookup.mapToColumn(param)
        op = opdict[ops]
        value = float(value)
        # Build the subquery
        valcount = self.session.query(self._spaxelclass.file_pk.label('valfile'),
                                      (func.count(self._spaxelclass.pk)).label('valcount')).\
            filter(op(attribute, value)).\
            group_by(self._spaxelclass.file_pk).subquery('goodhacount', with_labels=True)

        return valcount

    def _get_percent(self, fxn, **kwargs):
        ''' Query - Computes count comparisons

        Retrieves the number of objects that have satisfy a given expression
        in x% of good spaxels.  Expression is of the form
        Parameter Operand Value. This function is mapped to
        the "npergood" filter name.

        Syntax: fxnname(expression) operator value

        Parameters:
            fxn (str):
                The function condition used in the query filter

        Example:
            >>> fxn = 'npergood(junk.emline_gflux_ha_6564 > 25) >= 20'
            >>> Syntax: npergood() - function name
            >>>         npergood(expression) operator value
            >>>
            >>> Select objects that have Ha flux > 25 in more than
            >>> 20% of their (good) spaxels.
        '''

        # parse the function into name, condition, operator, and value
        name, condition, ops, value = self._parse_fxn(fxn)
        percent = float(value) / 100.
        op = opdict[ops]

        # Retrieve the necessary subqueries
        bincount = self._get_good_spaxels()
        valcount = self._get_count_of(condition)

        # Join to the main query
        self.query = self.query.join(bincount, bincount.c.binfile == self._spaxelclass.file_pk).\
            join(valcount, valcount.c.valfile == self._spaxelclass.file_pk).\
            filter(op(valcount.c.valcount, percent * bincount.c.goodcount))

        # Group the results by main default datadb parameters, so as not to include all spaxels
        newdefs = [d for d in self.default_params if 'spaxelprop' not in d]
        self.query = self._group_by(params=newdefs)

    def _parse_fxn(self, fxn):
        ''' Parse a fxn condition '''
        return fxn.fxnname, fxn.fxncond, fxn.op, fxn.value

    def _parse_expression(self, expr):
        ''' Parse an expression '''
        return expr.fullname, expr.op, expr.value

    def _check_dapall_query(self):
        ''' Checks if the query is on the DAPall table, and regroup the parameters plateifu'''

        isdapall = self._check_query('dapall')
        if isdapall:
            self.query = self._group_by()

