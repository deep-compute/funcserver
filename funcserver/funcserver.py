from basescript import BaseScript

import os
import gc
import sys
import json
import time
import code
import inspect
import logging
import resource
import string
import random
import threading
import msgpack
import cStringIO
import traceback
import urlparse
from multiprocessing.pool import ThreadPool

import statsd
import requests
import tornado.ioloop
import tornado.web
import tornado.websocket
import tornado.iostream
from tornado.template import BaseLoader, Template
from tornado.web import StaticFileHandler, HTTPError

MSG_TYPE_INFO = 0
MSG_TYPE_CONSOLE = 1
MSG_TYPE_LOG = 2

MAX_LOG_FILE_SIZE = 100 * 1024 * 1024 # 100MB

def disable_requests_debug_logs():
    # set the logging level of requests module to warning
    # otherwise it swamps with too many logs
    logging.getLogger('requests').setLevel(logging.WARNING)

def tag(*tags):
    '''
    Constructs a decorator that tags a function with specified
    strings (@tags). The tags on the decorated function are
    available via fn.tags
    '''
    def dfn(fn):
        _tags = getattr(fn, 'tags', set())
        _tags.update(tags)
        fn.tags = _tags
        return fn
    return dfn

def get_fn_tags(fn):
    return getattr(fn, 'tags', set())

def mime(mime):
    '''
    Constructs a decorator that sets the preferred mime type
    to be written in the http response when returning the
    function result.
    '''
    def dfn(fn):
        fn.mime = mime
        return fn
    return dfn

def raw(mime='application/octet-stream'):
    '''
    Constructs a decorator that marks the fn
    as raw response format
    '''
    def dfn(fn):
        tags = getattr(fn, 'tags', set())
        tags.add('raw')
        fn.tags = tags
        fn.mime = getattr(fn, 'mime', mime)
        return fn
    return dfn

class RPCCallException(Exception):
    pass

class BaseHandler(tornado.web.RequestHandler):
    def __init__(self, application, request, **kwargs):
        super(BaseHandler, self).__init__(application, request, **kwargs)
        a = self.application
        self.server = s = a.funcserver
        self.stats = s.stats
        self.log = s.log
        self.api = s.api

    def get_template_namespace(self):
        ns = super(BaseHandler, self).get_template_namespace()
        ns.update(sys.funcserver.define_template_namespace())
        return ns

class PyInterpreter(code.InteractiveInterpreter):
    def __init__(self, *args, **kwargs):
        code.InteractiveInterpreter.__init__(self, *args, **kwargs)
        self.output = []

    def write(self, data):
        self.output.append(data)

class WSConnection(tornado.websocket.WebSocketHandler):
    '''
    Websocket based communication channel between a
    client and the server.
    '''

    WRITE_BUFFER_THRESHOLD = 1 * 1024 * 1024 # 1MB

    def open(self, pysession_id):
        '''
        Called when client opens connection. Initialization
        is done here.
        '''
        self.id = id(self)
        self.funcserver = self.application.funcserver
        self.pysession_id = pysession_id

        # register this connection with node
        self.state = self.funcserver.websocks[self.id] = {'id': self.id, 'sock': self}

    def on_message(self, msg):
        '''
        Called when client sends a message.

        Supports a python debugging console. This forms
        the "eval" part of a standard read-eval-print loop.

        Currently the only implementation of the python
        console is in the WebUI but the implementation
        of a terminal based console is planned.
        '''

        msg = json.loads(msg)

        psession = self.funcserver.pysessions.get(self.pysession_id, None)
        if psession is None:
            interpreter = PyInterpreter(self.funcserver.define_python_namespace())
            psession = dict(interpreter=interpreter, socks=set([self.id]))
            self.funcserver.pysessions[self.pysession_id] = psession
        else:
            interpreter = psession['interpreter']
            psession['socks'].add(self.id)

        code = msg['code']
        msg_id = msg['id']

        stdout = sys.stdout
        try:
            sys.stdout = cStringIO.StringIO()
            interpreter.runsource(code)
            output = sys.stdout.getvalue() or interpreter.output
            if isinstance(output, list): output = ''.join(output)
            interpreter.output = []
        finally:
            sys.stdout = stdout

        msg = {'type': MSG_TYPE_CONSOLE, 'id': msg_id, 'data': output}
        self.send_message(msg)

    def on_close(self):
        '''
        Called when client closes this connection. Cleanup
        is done here.
        '''

        if self.id in self.funcserver.websocks:
            self.funcserver.websocks[self.id] = None
            ioloop = tornado.ioloop.IOLoop.instance()
            ioloop.add_callback(lambda: self.funcserver.websocks.pop(self.id, None))

        psession = self.funcserver.pysessions.get(self.pysession_id, None)
        if psession:
            psession['socks'].remove(self.id)
            if not psession['socks']:
                del self.funcserver.pysessions[self.pysession_id]

    def send_message(self, msg, binary=False):
        # TODO: check if following two lines are required
        # tornado documentation seems to indicate that
        # this might be handled internally.
        if not isinstance(msg, str):
            msg = json.dumps(msg)

        try:
            if self.ws_connection:
                self.write_message(msg, binary=binary)
        except tornado.iostream.StreamClosedError:
            self.on_close()

    @property
    def is_buffer_full(self):
        bsize = sum([len(x) for x in self.stream._write_buffer])
        return bsize >= self.WRITE_BUFFER_THRESHOLD

    def _msg_from(self, msg):
        return {'type': msg.get('type', ''), 'id': msg['id']}


def call(fn):
    ioloop = tornado.ioloop.IOLoop.instance()
    ioloop.add_callback(fn)


def make_handler(template, handler):
    class SimpleHandler(handler):
        def get(self):
            return self.render(template)

    return SimpleHandler


def resolve_path(path):
    return path if os.path.isabs(path) else os.path.join(os.path.dirname(__file__), path)


class TemplateLoader(BaseLoader):
    def __init__(self, dirs=None, **kwargs):
        super(TemplateLoader, self).__init__(**kwargs)
        self.dirs = dirs or []

    def add_dir(self, d):
        self.dirs.append(d)

    def del_dir(self, d):
        self.dirs.remove(d)

    def resolve_path(self, name, parent_path=None):
        for d in reversed(self.dirs):
            p = os.path.join(d, name)
            if not os.path.exists(p): continue
            return os.path.abspath(p)

        return name

    def _create_template(self, name):
        f = open(name, 'rb')
        template = Template(f.read(), name=name, loader=self)
        f.close()
        return template


class CustomStaticFileHandler(StaticFileHandler):
    PATHS = []

    @classmethod
    def get_absolute_path(cls, root, path):
        for p in reversed(cls.PATHS):
            ap = os.path.join(p, path)
            if not os.path.exists(ap):
                continue
            return ap

        return path

    def validate_absolute_path(self, root, absolute_path):
        if (os.path.isdir(absolute_path) and
                self.default_filename is not None):
            # need to look at the request.path here for when path is empty
            # but there is some prefix to the path that was already
            # trimmed by the routing
            if not self.request.path.endswith("/"):
                self.redirect(self.request.path + "/", permanent=True)
                return
            absolute_path = os.path.join(absolute_path, self.default_filename)
        if not os.path.exists(absolute_path):
            raise HTTPError(404)
        if not os.path.isfile(absolute_path):
            raise HTTPError(403, "%s is not a file", self.path)
        return absolute_path

class RPCHandler(BaseHandler):

    def _get_apifn(self, fn_name):
        obj = self.api
        for part in fn_name.split('.'):
            obj = getattr(obj, part)
        return obj

    def _clean_kwargs(self, kwargs, fn):
        '''
        Remove unexpected keyword arguments from the
        set of received keyword arguments.
        '''
        # Do not do the cleaning if server config
        # doesnt ask to ignore
        if not self.server.IGNORE_UNEXPECTED_KWARGS:
            return kwargs

        expected_kwargs = set(inspect.getargspec(fn).args)
        got_kwargs = set(kwargs.keys())
        unexpected_kwargs = got_kwargs - expected_kwargs
        for k in unexpected_kwargs:
            del kwargs[k]

        return kwargs

    def _handle_single_call(self, request, m):
        fn_name = m.get('fn', None)
        t = time.time()

        tags = { "fn": fn_name or "unknown", "success": False }
        try:
            fn = self._get_apifn(fn_name)
            args = m['args']
            kwargs = self._clean_kwargs(m['kwargs'], fn)

            self.server.on_api_call_start(fn_name, args, kwargs, self)
            if self.get_status() == 304:
                return

            r = fn(*args, **kwargs)
            r = {'success': True, 'result': r}

            tags['success'] = True

        except Exception, e:
            tags['success'] = False

            self.log.exception("RPC failed",
                fn=fn_name, args=m.get('args'), kwargs=m.get('kwargs'),
            )
            r = {'success': False, 'result': repr(e)}

        finally:
            tdiff = (time.time() - t) * 1000
            (
                self.stats.measure('api', **tags)
                    .count(invoked=1)
                    .time(duration=tdiff)
            )

        try:
            _r = self.server.on_api_call_end(fn_name, args, kwargs, self, r)
            if _r is not None:
                r = _r
        except (SystemExit, KeyboardInterrupt): raise
        except:
            self.log.exception("on_api_call_end failed",
                fn=fn_name, args=args, kwargs=kwargs,
            )

        return r

    def _handle_call(self, request, fn, m, protocol):
        if fn != '__batch__':
            r = self._handle_single_call(request, m)
        else:
            # Batch calls
            r = []
            for call in m['calls']:
                _r = self._handle_single_call(request, call)

                # If the func invoked above is a streaming function, then fail
                # this operation as we don't handle streaming functions in batch mode
                if inspect.isgenerator(_r.get('result')):
                    raise APIException('Cannot invoke streaming API fn in batch mode')

                if isinstance(_r, dict) and 'success' in _r:
                    _r = _r['result'] if _r['success'] else None
                r.append(_r)

        if self.get_status() == 304:
            return

        # Get the API function object
        fnobj = self._get_apifn(fn) if fn != '__batch__' else (lambda: 0)

        # Set response header based on chosen serialization mechanism
        mime = getattr(fnobj, 'mime', self.get_mime(protocol))
        self.set_header('Content-Type', mime)

        is_raw = 'raw' in get_fn_tags(fnobj)
        serializer = (lambda x: x) if is_raw else self.get_serializer(protocol)

        if fn == '__batch__' or not r['success']:
            r = serializer(r)
            self.set_header('Content-Length', len(r))
            self.write(r)
            return

        result = r['result']

        if not inspect.isgenerator(result):
            # Full response is available - Write it out in one shot
            r = serializer(r)
            self.set_header('Content-Length', len(r))
            self.write(r)
            return

        # Streaming response - iterate and write out
        for part in result:
            part = serializer(part)
            self.write(part)
            sep = '\n' if is_raw else self.get_record_separator(protocol)
            if sep: self.write(sep)
            self.flush()

    def get_record_separator(self, protocol):
        return {'msgpack': '',
                'json': '\n',
                'python': '\n'}.get(protocol, self.server.SERIALIZER_RECORD_SEP)

    def get_serializer(self, name):
        return {'msgpack': msgpack.packb,
                'json': json.dumps,
                'python': repr}.get(name, self.server.SERIALIZER)

    def get_deserializer(self, name):
        return {'msgpack': msgpack.packb,
                'json': json.loads,
                'python': eval}.get(name, self.server.DESERIALIZER)

    def get_mime(self, name):
        return {'msgpack': 'application/x-msgpack',
                'json': 'application/json',
                'python': 'application/x-python'}\
                .get(name, self.server.MIME)

    def _handle_call_wrapper(self, request, fn, m, protocol):
        try:
            return self._handle_call(request, fn, m, protocol)
        except Exception, e:
            self.log.exception("RPC failed", fn=m.get('fn'),
                args=m.get('args'), kwargs=m.get('kwargs'),
            )
            self.clear()
            self.set_status(500)

        finally:
            self.finish()

    @tornado.web.asynchronous
    def post(self, protocol='default'):
        m = self.get_deserializer(protocol)(self.request.body)
        fn = m['fn']
        self.server.threadpool.apply_async(lambda: self._handle_call_wrapper(self.request, fn, m, protocol))

    def failsafe_json_decode(self, v):
        try: v = json.loads(v)
        except ValueError: pass
        return v

    @tornado.web.asynchronous
    def get(self, protocol='default'):
        D = self.failsafe_json_decode
        args = dict([(k, D(v[0]) if len(v) == 1 else [D(x) for x in v])\
                    for k, v in self.request.arguments.iteritems()])

        fn = args.pop('fn')
        m = dict(kwargs=args, fn=fn, args=[])
        self.server.threadpool.apply_async(lambda: self._handle_call_wrapper(self.request, fn, m, protocol))


class Server(BaseScript):
    NAME = 'FuncServer'
    DESC = 'Default Functionality Server'
    DEFAULT_PORT = 9345
    VIRTUAL_HOST = r'.*'

    STATIC_PATH = 'static'
    TEMPLATE_PATH = 'templates'

    APP_CLASS = tornado.web.Application
    RPC_HANDLER_CLASS = RPCHandler

    SERIALIZER = staticmethod(msgpack.packb)
    SERIALIZER_RECORD_SEP = ''
    DESERIALIZER = staticmethod(msgpack.unpackb)
    MIME = 'application/x-msgpack'

    IGNORE_UNEXPECTED_KWARGS = False

    # Number of worker threads in the threadpool
    THREADPOOL_WORKERS = 32

    DISABLE_REQUESTS_DEBUG_LOGS = True

    def dump_stacks(self):
        '''
        Dumps the stack of all threads. This function
        is meant for debugging. Useful when a deadlock happens.

        borrowed from: http://blog.ziade.org/2012/05/25/zmq-and-gevent-debugging-nightmares/
        '''

        dump = []

        # threads
        threads = dict([(th.ident, th.name)
                            for th in threading.enumerate()])

        for thread, frame in sys._current_frames().items():
            if thread not in threads: continue
            dump.append('Thread 0x%x (%s)\n' % (thread, threads[thread]))
            dump.append(''.join(traceback.format_stack(frame)))
            dump.append('\n')

        return ''.join(dump)

    @property
    def name(self):
        return '.'.join([x for x in (self.NAME, self.args.name) if x])

    def new_pysession(self):
        chars = list(set(string.letters + string.digits))
        name = ''.join([random.choice(chars) for i in xrange(10)])
        if name in self.pysessions:
            return self.new_pysession()
        return name

    def define_args(self, parser):
        super(Server, self).define_args(parser)

        parser.add_argument('--port', default=self.DEFAULT_PORT,
            type=int, help='port to listen on for server')
        parser.add_argument('--debug', action='store_true',
                help='When enabled, auto reloads server on code change')

    def define_log_pre_format_hooks(self):
        """
        adds a hook to send to websocket if the run command was selected
        """
        hooks = super(Server, self).define_log_pre_format_hooks()
        # NOTE enabling logs only on debug mode
        if self.args.func == self.run and self.args.debug:
            hooks.append(self._send_log_to_ws)

        return hooks

    def _send_log_to_ws(self, msg):
        websocks = getattr(self, "websocks", None)
        if websocks is None or len(websocks) == 0:
            return

        msg = {'type': MSG_TYPE_LOG, 'id': self.log_id, 'data': msg}

        bad_ws = []

        for _id, ws in websocks.iteritems():
            if ws is None: bad_ws.append(_id); continue
            ws['sock'].send_message(msg)

        for _id in bad_ws: del self.websocks[_id]

        self.log_id += 1

    def prepare_base_handlers(self):
        # Tornado URL handlers for core functionality

        debug_mode_only = [
            (r'/ws/(.*)', WSConnection),
            (r'/logs', make_handler('logs.html', BaseHandler)),
            (r'/console', make_handler('console.html', BaseHandler)),
        ]

        others = [
            (r'/', make_handler('home.html', BaseHandler)),
            (r'/rpc(?:/([^/]*)/?)?', self.RPC_HANDLER_CLASS),
        ]

        if self.args.debug:
            return debug_mode_only + others
        else:
            return others

    def prepare_handlers(self):
        # Tornado URL handlers for additional functionality
        return []

    def prepare_template_loader(self, loader):
        # add additional template dirs by using
        # loader.add_dir(path)
        return loader

    def prepare_static_paths(self, paths):
        # add static paths that can contain
        # additional of override files
        # eg: paths.append(PATH)
        return paths

    def prepare_nav_tabs(self, nav_tabs):
        # Add additional tab buttons in the UI toolbar
        # eg: nav_tabs.append(('MyTab', '/mytab'))
        return nav_tabs

    def define_python_namespace(self):
        return {'server': self, 'logging': logging, 'call': call, 'api': self.api}

    def define_template_namespace(self):
        return self.define_python_namespace()

    def on_api_call_start(self, fn, args, kwargs, handler):
        pass

    def on_api_call_end(self, fn, args, kwargs, handler, result):
        return result

    def prepare_api(self):
        '''
        Prepare the API object that is exposed as
        functionality by the Server
        '''
        return None

    def run(self):
        """ prepares the api and starts the tornado funcserver """
        self.log_id = 0

        # all active websockets and their state
        self.websocks = {}

        # all active python interpreter sessions
        self.pysessions = {}

        if self.DISABLE_REQUESTS_DEBUG_LOGS:
            disable_requests_debug_logs()

        self.threadpool = ThreadPool(self.THREADPOOL_WORKERS)

        self.api = None

        # tornado app object
        base_handlers = self.prepare_base_handlers()
        handlers = self.prepare_handlers()
        self.template_loader = TemplateLoader([resolve_path(self.TEMPLATE_PATH)])
        _ = self.prepare_template_loader(self.template_loader)
        if _ is not None: self.template_loader = _

        shclass = CustomStaticFileHandler
        shclass.PATHS.append(resolve_path(self.STATIC_PATH))
        _ = self.prepare_static_paths(shclass.PATHS)
        if _ is not None: shclass.PATHS = _

        self.static_handler_class = shclass

        self.nav_tabs = [('Home', '/')]
        if self.args.debug:
            self.nav_tabs += [('Console', '/console'), ('Logs', '/logs')]
        self.nav_tabs = self.prepare_nav_tabs(self.nav_tabs)

        settings = {
            'static_path': '<DUMMY-INEXISTENT-PATH>',
            'static_handler_class': self.static_handler_class,
            'template_loader': self.template_loader,
            'compress_response': True,
            'debug': self.args.debug,
        }

        all_handlers = handlers + base_handlers
        self.app = self.APP_CLASS(**settings)
        self.app.add_handlers(self.VIRTUAL_HOST, all_handlers)

        sys.funcserver = self.app.funcserver = self

        self.api = self.prepare_api()
        if self.api is not None and not hasattr(self.api, 'log'):
            self.api.log = self.log

        if self.args.port != 0:
            self.app.listen(self.args.port)

        tornado.ioloop.IOLoop.instance().start()

def _passthrough(name):
    def fn(self, *args, **kwargs):
        p = self.prefix + '.' + name
        if self.parent is None:
            return self._call(p, args, kwargs)
        else:
            return self.parent._call(p, args, kwargs)
    return fn

class Client(object):
    SERIALIZER = staticmethod(msgpack.packb)
    DESERIALIZER = staticmethod(msgpack.unpackb)

    DISABLE_REQUESTS_DEBUG_LOGS = True

    def __init__(self, server_url, prefix=None, parent=None, is_batch=False):
        self.server_url = server_url
        self.rpc_url = urlparse.urljoin(server_url, 'rpc')
        self.is_batch = is_batch
        self.prefix = prefix
        self.parent = parent
        self._calls = []

        if self.DISABLE_REQUESTS_DEBUG_LOGS:
            disable_requests_debug_logs()

    def __getattr__(self, attr):
        prefix = self.prefix + '.' + attr if self.prefix else attr
        return self.__class__(self.server_url, prefix=prefix,
                parent=self.parent or self,
                is_batch=self.is_batch)

    def __call__(self, *args, **kwargs):
        if self.parent is None:
            return self._call(self.prefix, args, kwargs)
        else:
            return self.parent._call(self.prefix, args, kwargs)

    def _call(self, fn, args, kwargs):
        if not self.is_batch:
            return self._do_single_call(fn, args, kwargs)
        else:
            self._calls.append(dict(fn=fn, args=args, kwargs=kwargs))

    __getitem__ = _passthrough('__getitem__')
    __setitem__ = _passthrough('__setitem__')
    __delitem__ = _passthrough('__delitem__')
    __contains__ = _passthrough('__contains__')
    __len__ = _passthrough('__len__')

    def __nonzero__(self): return True

    def set_batch(self):
        self.is_batch = True

    def unset_batch(self):
        self.is_batch = False

    def _do_single_call(self, fn, args, kwargs):
        m = self.SERIALIZER(dict(fn=fn, args=args, kwargs=kwargs))
        req = requests.post(self.rpc_url, data=m)
        res = self.DESERIALIZER(req.content)

        if not res['success']:
            raise RPCCallException(res['result'])
        else:
            return res['result']

    def execute(self):
        if not self._calls: return

        m = dict(fn='__batch__', calls=self._calls)
        m = self.SERIALIZER(m)
        req = requests.post(self.rpc_url, data=m)
        res = self.DESERIALIZER(req.content)
        self._calls = []

        return res

if __name__ == '__main__':
    Server().start()
