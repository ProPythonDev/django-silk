import json
import logging

from django.core.urlresolvers import reverse, NoReverseMatch
from django.db.models.sql.compiler import SQLCompiler
from django.utils import timezone
from django.utils.encoding import DjangoUnicodeDecodeError
import sys

from silk import models
from silk.collector import DataCollector
from silk.config import SilkyConfig
from silk.profiling import dynamic
from silk.sql import execute_sql


Logger = logging.getLogger('silk')

content_types_json = ['application/json',
                      'application/x-javascript',
                      'text/javascript',
                      'text/x-javascript',
                      'text/x-json']
content_type_form = ['multipart/form-data',
                     'application/x-www-form-urlencoded']
content_type_html = ['text/html']
content_type_css = ['text/css']


def silky_reverse(name, *args, **kwargs):
    try:
        r = reverse('silk:%s' % name, *args, **kwargs)
    except NoReverseMatch:
        # In case user forgets to set namespace, but also fixes Django 1.5 tests on Travis
        # Hopefully if user has forgotten to add namespace there are no clashes with their own
        # view names but I don't think there is really anything can do about this.
        r = reverse(name, *args, **kwargs)
    return r


def _should_intercept(request):
    """we want to avoid recording any requests/sql queries etc that belong to Silky"""
    fpath = silky_reverse('requests')
    path = '/'.join(fpath.split('/')[0:-1])
    silky = request.path.startswith(path)
    ignored = request.path in SilkyConfig().SILKY_IGNORE_PATHS
    return not (silky or ignored)


class RequestModelFactory(object):
    """Produce Request models from Django request objects"""

    def __init__(self, request):
        super(RequestModelFactory, self).__init__()
        self.request = request

    def content_type(self):
        content_type = self.request.META.get('CONTENT_TYPE', '')
        if content_type:
            content_type = content_type.split(';')[0]
        return content_type

    def encoded_headers(self):
        """
        From Django docs (https://docs.djangoproject.com/en/1.6/ref/request-response/#httprequest-objects):

        "With the exception of CONTENT_LENGTH and CONTENT_TYPE, as given above, any HTTP headers in the request are converted to
        META keys by converting all characters to uppercase, replacing any hyphens with underscores and adding an HTTP_ prefix
        to the name. So, for example, a header called X-Bender would be mapped to the META key HTTP_X_BENDER."
        """
        headers = {}
        for k, v in self.request.META.items():
            if k.startswith('HTTP') or k in ('CONTENT_TYPE', 'CONTENT_LENGTH'):
                splt = k.split('_')
                if splt[0] == 'HTTP':
                    splt = splt[1:]
                k = '-'.join(splt)
                headers[k] = v
        if SilkyConfig().SILKY_HIDE_COOKIES:
            try:
                del headers['COOKIE']
            except KeyError:
                pass
        return json.dumps(headers)

    def _body(self, raw_body):
        """
        Encode body as JSON if possible so can be used as a dictionary in generation
        of curl/django test client code
        """
        content_type = self.content_type()
        body = ''
        if content_type in content_type_form:
            body = self.request.POST
            body = json.dumps(dict(body), sort_keys=True, indent=4)
        elif content_type in content_types_json:
            body = json.dumps(json.loads(raw_body), sort_keys=True, indent=4)
        return body

    def body(self):
        raw_body = self.request.body
        max_size = SilkyConfig().SILKY_MAX_REQUEST_BODY_SIZE
        body = ''
        if max_size > -1:
            Logger.debug('A max request size is set so checking size')
            size = sys.getsizeof(raw_body, default=None)
            request_identifier = self.request.path
            if not size:
                Logger.error('No way in which to get size of request body for %s, will ignore it', request_identifier)
            elif size <= max_size:
                Logger.debug('Request %s has body of size %d which is less than %d so will save the body' % (request_identifier, size, max_size))
                body = self._body(raw_body)
            else:
                Logger.debug('Request %s has body of size %d which is greater than %d, therefore ignoring' % (request_identifier, size, max_size))
                raw_body = None
        else:
            Logger.debug('No maximum request body size is set, continuing.')
            body = self._body(raw_body)
        return body, raw_body  # Can't read body twice.

    def query_params(self):
        query_params = self.request.GET
        encoded_query_params = ''
        if query_params:
            query_params_dict = dict(zip(query_params.keys(), query_params.values()))
            encoded_query_params = json.dumps(query_params_dict)
        return encoded_query_params

    def construct_request_model(self):
        body, raw_body = self.body()
        query_params = self.query_params()
        request_model = models.Request.objects.create(
            path=self.request.path,
            encoded_headers=self.encoded_headers(),
            method=self.request.method,
            query_params=query_params,
            body=body)
        try:
            request_model.raw_body = raw_body
        except DjangoUnicodeDecodeError:
            Logger.debug('NYI: Binary request bodies')  # TODO
        Logger.debug('Created new request model with pk %s' % request_model.pk)
        return request_model


class ResponseModelFactory(object):
    """given a response object, craft the silk response model"""
    def __init__(self, response):
        super(ResponseModelFactory, self).__init__()
        self.response = response


    def construct_response_model(self):
        silk_request = DataCollector().request
        assert silk_request, 'Cant construct a response model if there is no request model'
        Logger.debug('Creating response model for request model with pk %s' % silk_request.pk)
        body = ''
        content_type = self.response.get('Content-Type', '').split(';')[0]
        content = self.response.content
        max_body_size = SilkyConfig().SILKY_MAX_RESPONSE_BODY_SIZE
        if max_body_size > -1:
            Logger.debug('Max size of response body defined so checking')
            size = sys.getsizeof(content, None)
            if not size:
                Logger.error('Could not get size of response body. Ignoring')
                content = ''
            else:
                if size > max_body_size:
                    content = ''
                    Logger.debug('Size of %d for %s is bigger than %d so ignoring response body' % (size, silk_request.path, max_body_size))
                else:
                    Logger.debug('Size of %d for %s is less than %d so saving response body' % (size, silk_request.path, max_body_size))
        try:  # py3
            content = content.decode('UTF-8')
        except AttributeError:  # py2
            pass
        if content_type in content_types_json:
            # TODO: Perhaps theres a way to format the JSON without parsing it?
            try:
                body = json.dumps(json.loads(content), sort_keys=True, indent=4)
            except (TypeError, ValueError):
                Logger.warn('Response to request with pk %s has content type %s but was unable to parse it' % (silk_request.pk, content_type))
        raw_headers = self.response._headers
        headers = {}
        for k, v in raw_headers.items():
            try:
                header, val = v
            except ValueError:
                header, val = k, v
            finally:
                headers[header] = val
        silky_response = models.Response.objects.create(request=silk_request,
                                                        status_code=self.response.status_code,
                                                        encoded_headers=json.dumps(headers),
                                                        body=body)
        try:
            silky_response.raw_body = content
        except DjangoUnicodeDecodeError:
            Logger.debug('NYI: Saving of binary response body')  # TODO
        return silky_response



class SilkyMiddleware(object):
    def __init__(self):
        super(SilkyMiddleware, self).__init__()

    def _apply_dynamic_mappings(self):
        dynamic_profile_configs = SilkyConfig().SILKY_DYNAMIC_PROFILING
        for conf in dynamic_profile_configs:
            module = conf.get('module')
            function = conf.get('function')
            start_line = conf.get('start_line')
            end_line = conf.get('end_line')
            name = conf.get('name')
            if module and function:
                if start_line and end_line:  # Dynamic context manager
                    dynamic.inject_context_manager_func(module=module,
                                                        func=function,
                                                        start_line=start_line,
                                                        end_line=end_line,
                                                        name=name)
                else:  # Dynamic decorator
                    dynamic.profile_function_or_method(module=module,
                                                       func=function,
                                                       name=name)
            else:
                raise KeyError('Invalid dynamic mapping %s' % conf)

    def process_request(self, request):
        request_model = None
        if _should_intercept(request):
            self._apply_dynamic_mappings()
            if not hasattr(SQLCompiler, '_execute_sql'):
                SQLCompiler._execute_sql = SQLCompiler.execute_sql
                SQLCompiler.execute_sql = execute_sql
            request_model = RequestModelFactory(request).construct_request_model()
        DataCollector().configure(request_model)

    def process_response(self, request, response):
        if _should_intercept(request):
            meta_start_time = None
            if SilkyConfig().SILKY_META:
                meta_start_time = timezone.now()
            collector = DataCollector()
            silk_request = collector.request
            if silk_request:
                silk_response = ResponseModelFactory(response).construct_response_model()
                silk_response.save()
                silk_request.end_time = timezone.now()
                collector.finalise()
                if SilkyConfig().SILKY_META:
                    meta_end_time = timezone.now()
                    silk_request.meta_start_time = meta_start_time
                    silk_request.meta_end_time = meta_end_time
                silk_request.save()
            else:
                Logger.error('No request model was available when processing response. Did something go wrong in process_request/process_view?')
        return response