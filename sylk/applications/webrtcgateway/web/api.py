
import json
import random
import re
import uuid

from application.python import Null
from application.notification import IObserver, NotificationCenter
from autobahn.twisted.websocket import WebSocketServerProtocol, WebSocketServerFactory
from autobahn.websocket import http
from eventlib import coros, proc
from eventlib.twistedutil import block_on
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI, SIPCoreError
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp
from twisted.internet import reactor
from zope.interface import implements

from sylk.applications.webrtcgateway.configuration import GeneralConfig
from sylk.applications.webrtcgateway.logger import log
from sylk.applications.webrtcgateway.util import GreenEvent


SYLK_WS_PROTOCOL = 'sylkRTC-1'
SIP_PREFIX_RE = re.compile('^sips?:')


class AccountInfo(object):
    def __init__(self, id, password):
        self.id = id
        self.password = password
        self.registration_state = None
        self.janus_handle_id = None

    @property
    def uri(self):
        return 'sip:%s' % self.id


class SessionPartyIdentity(object):
    def __init__(self, uri, display_name=''):
        self.uri = uri
        self.display_name = display_name


class SessionInfoBase(object):
    type = None    # override in subclass

    def __init__(self, id):
        self.id = id
        self.direction = None
        self.state = None
        self.account_id = None
        self.local_identity = None     # instance of SessionPartyIdentity
        self.remote_identity = None    # instance of SessionPartyIdentity

    def init_outgoing(self, account_id, destination):
        self.account_id = account_id
        self.direction = 'outgoing'
        self.state = 'connecting'
        self.local_identity = SessionPartyIdentity(account_id)
        self.remote_identity = SessionPartyIdentity(destination)

    def init_incoming(self, account_id, originator, originator_display_name=''):
        self.account_id = account_id
        self.direction = 'incoming'
        self.state = 'connecting'
        self.local_identity = SessionPartyIdentity(account_id)
        self.remote_identity = SessionPartyIdentity(originator, originator_display_name)


class JanusSessionInfo(SessionInfoBase):
    type = 'janus'

    def __init__(self, id):
        super(JanusSessionInfo, self).__init__(id)
        self.janus_handle_id = None


class Operation(object):
    __slots__ = ('name', 'data')

    def __init__(self, name, data):
        self.name = name
        self.data = data


class APIError(Exception):
    pass


class SylkWebSocketServerProtocol(WebSocketServerProtocol):
    janus_session_id = None
    accounts_map = None           # account ID -> account
    account_handles_map = None    # Janus handle ID -> account
    sessions_map = None           # session ID -> session
    session_handles_map = None    # Janus handle ID -> session
    ready_event = None
    resolver = None
    proc = None
    operations_queue = None

    def onConnect(self, request):
        log.msg('Incoming connection from %s (origin %s)' % (request.peer, request.origin))
        if SYLK_WS_PROTOCOL not in request.protocols:
            log.msg('Rejecting connection, remote does not support our sub-protocol')
            raise http.HttpException(http.NOT_ACCEPTABLE[0], 'No compatible protocol specified')
        if not self.backend.ready:
            log.msg('Rejecting connection, backend is not connected')
            raise http.HttpException(http.SERVICE_UNAVAILABLE[0], 'Backend is not connected')
        return SYLK_WS_PROTOCOL

    def onOpen(self):
        self.factory.connections.add(self)
        self.accounts_map = {}
        self.account_handles_map = {}
        self.sessions_map = {}
        self.session_handles_map = {}
        self.ready_event = GreenEvent()
        self.resolver = DNSLookup()
        self.proc = proc.spawn(self._operations_handler)
        self.operations_queue = coros.queue()
        self._create_janus_session()

    def onMessage(self, payload, isBinary):
        if isBinary:
            log.warn('Received invalid binary message')
            return
        if GeneralConfig.trace_websocket:
            self.factory.ws_logger.msg("IN", ISOTimestamp.now(), payload)
        try:
            data = json.loads(payload)
        except Exception, e:
            log.warn('Error parsing WebSocket payload: %s' % e)
            return
        try:
            request_type = data.pop('sylkrtc')
        except KeyError:
            log.warn('Error getting WebSocket message type')
            return
        self.ready_event.wait()
        op = Operation(request_type.lower(), data)
        self.operations_queue.send(op)

    def onClose(self, wasClean, code, reason):
        if self.ready_event is None:
            # Very early connection closed, onOpen wasn't even called
            return
        log.msg('Connection from %s closed' % self.transport.getPeer())
        self.factory.connections.discard(self)
        if self.ready_event.is_set():
            assert self.janus_session_id is not None
            self.backend.janus_stop_keepalive(self.janus_session_id)
            self.backend.janus_destroy_session(self.janus_session_id)
        if self.proc is not None:
            self.proc.kill()
            self.proc = None
        # cleanup
        self.ready_event.clear()
        self.accounts_map.clear()
        self.account_handles_map.clear()
        self.sessions_map.clear()
        self.session_handles_map.clear()
        self.janus_session_id = None

    def disconnect(self, code=1000, reason=u''):
        self.sendClose(code, reason)

    # internal methods (not overriding / implementing the protocol API)

    def _send_data(self, data):
        if GeneralConfig.trace_websocket:
            self.factory.ws_logger.msg("OUT", ISOTimestamp.now(), data)
        self.sendMessage(data, False)

    def _cleanup_session(self, session):
        @run_in_green_thread
        def do_cleanup():
            if self.janus_session_id is None:
                # The connection was closed, there is noting to do here
                return
            self.sessions_map.pop(session.id)
            if session.direction == 'outgoing':
                # Destroy plugin handle for outgoing sessions. For incoming ones it's the
                # same as the account handle, so don't
                block_on(self.backend.janus_detach(self.janus_session_id, session.janus_handle_id))
                self.backend.janus_set_event_handler(session.janus_handle_id, None)
            self.session_handles_map.pop(session.janus_handle_id)

        # give it some time to receive other hangup events
        reactor.callLater(2, do_cleanup)

    @run_in_green_thread
    def _create_janus_session(self):
        if self.ready_event.is_set():
            data = dict(sylkrtc='event', event='ready')
            self._send_data(json.dumps(data))
            return
        try:
            self.janus_session_id = block_on(self.backend.janus_create_session())
            self.backend.janus_start_keepalive(self.janus_session_id)
        except Exception, e:
            log.warn('Error creating session, disconnecting: %s' % e)
            self.disconnect(3000, unicode(e))
            return
        data = dict(sylkrtc='event', event='ready')
        self._send_data(json.dumps(data))
        self.ready_event.set()

    def _lookup_sip_proxy(self, account):
        sip_uri = SIPURI.parse('sip:%s' % account)

        # The proxy dance: Sofia-SIP seems to do a DNS lookup per SIP message when a domain is passed
        # as the proxy, so do the resolution ourselves and give it pre-resolver proxy URL. Since we use
        # caching to avoid long delays, we randomize the results matching the highest priority route's
        # transport.
        proxy = GeneralConfig.outbound_sip_proxy
        if proxy is not None:
            proxy_uri = SIPURI(host=proxy.host,
                               port=proxy.port,
                               parameters={'transport': proxy.transport})
        else:
            proxy_uri = SIPURI(host=sip_uri.host)
        settings = SIPSimpleSettings()
        try:
            routes = self.resolver.lookup_sip_proxy(proxy_uri, settings.sip.transport_list).wait()
        except DNSLookupError, e:
            raise DNSLookupError('DNS lookup error: %s' % e)
        if not routes:
            raise DNSLookupError('DNS lookup error: no results found')

        # Get all routes with the highest priority transport and randomly pick one
        route = random.choice([r for r in routes if r.transport==routes[0].transport])

        # Build a proxy URI Sofia-SIP likes
        return '%s:%s:%d%s' % ('sips' if route.transport=='tls' else 'sip',
                               route.address,
                               route.port,
                               ';transport=%s' % route.transport if route.transport != 'tls' else '')

    def _handle_janus_event(self, handle_id, event_type, event):
        op = Operation('janus-event', data=dict(handle_id=handle_id, event_type=event_type, event=event))
        self.operations_queue.send(op)

    def _operations_handler(self):
        while True:
            op = self.operations_queue.wait()
            handler = getattr(self, '_OH_%s' % op.name.replace('-', '_'), Null)
            try:
                handler(op.data)
            except Exception:
                log.err()
            del op
            del handler

    def _OH_account_add(self, data):
        transaction = data.get('transaction', None)
        if transaction is None:
            log.warn('Transaction not specified!')
            return

        try:
            try:
                account = data['account']
                password = data['password']
            except KeyError:
                raise APIError('Invalid parameters: "account" and "password" must be specified')

            if account in self.accounts_map:
                log.warn('Account %s already added' % account)
                data = dict(sylkrtc='error', transaction=transaction, error='Account already added')
                self._send_data(json.dumps(data))
                return

            # Validate URI
            uri = 'sip:%s' % account
            try:
                sip_uri = SIPURI.parse(uri)
            except SIPCoreError:
                raise APIError('Invalid account specified: %s' % account)
            if not {'*', sip_uri.host}.intersection(GeneralConfig.sip_domains):
                raise APIError('SIP domain not allowed: %s' % sip_uri.host)

            # Create and store our mapping
            account_info = AccountInfo(account, password)
            self.accounts_map[account_info.id] = account_info

            data = dict(sylkrtc='ack', transaction=transaction)
            self._send_data(json.dumps(data))
            log.msg('Account %s added' % account)
        except APIError, e:
            log.error('account_add: %s' % e)
            data = dict(sylkrtc='error', transaction=transaction, error=str(e))
            self._send_data(json.dumps(data))
        except Exception, e:
            log.error('Unexpected error in account_add: %s' % e)
            data = dict(sylkrtc='error', transaction=transaction, error=str(e))
            self._send_data(json.dumps(data))

    def _OH_account_remove(self, data):
        transaction = data.get('transaction', None)
        if transaction is None:
            log.warn('Transaction not specified!')
            return

        try:
            try:
                account = data['account']
            except KeyError:
                raise APIError('Invalid parameters: "account" must be specified')

            try:
                account_info = self.accounts_map.pop(account)
            except KeyError:
                raise APIError('Unknown account specified: %s' % account)

            handle_id = account_info.janus_handle_id
            if handle_id is not None:
                block_on(self.backend.janus_detach(self.janus_session_id, handle_id))
                self.backend.janus_set_event_handler(handle_id, None)
                self.account_handles_map.pop(handle_id)

            data = dict(sylkrtc='ack', transaction=transaction)
            self._send_data(json.dumps(data))
            log.msg('Account %s removed' % account)
        except APIError, e:
            log.error('account_remove: %s' % e)
            data = dict(sylkrtc='error', transaction=transaction, error=str(e))
            self._send_data(json.dumps(data))
        except Exception, e:
            log.error('Unexpected error in account_remove: %s' % e)
            data = dict(sylkrtc='error', transaction=transaction, error=str(e))
            self._send_data(json.dumps(data))

    def _OH_account_register(self, data):
        transaction = data.get('transaction', None)
        if transaction is None:
            log.warn('Transaction not specified!')
            return

        try:
            try:
                account = data['account']
            except KeyError:
                raise APIError('Invalid parameters: "account" must be specified')

            try:
                account_info = self.accounts_map[account]
            except KeyError:
                raise APIError('Unknown account specified: %s' % account)

            proxy = self._lookup_sip_proxy(account)

            handle_id = account_info.janus_handle_id
            if handle_id is not None:
                # Destroy the existing plugin handle
                block_on(self.backend.janus_detach(self.janus_session_id, handle_id))
                self.backend.janus_set_event_handler(handle_id, None)
                self.account_handles_map.pop(handle_id)
                account_info.janus_handle_id = None

            # Create a plugin handle
            handle_id = block_on(self.backend.janus_attach(self.janus_session_id, 'janus.plugin.sip'))
            self.backend.janus_set_event_handler(handle_id, self._handle_janus_event)
            account_info.janus_handle_id = handle_id
            self.account_handles_map[handle_id] = account_info

            data = {'request': 'register',
                    'username': account_info.uri,
                    'ha1_secret': account_info.password,
                    'proxy': proxy}
            block_on(self.backend.janus_message(self.janus_session_id, handle_id, data))

            data = dict(sylkrtc='ack', transaction=transaction)
            self._send_data(json.dumps(data))
            log.msg('Account %s will register' % account)
        except APIError, e:
            log.error('account-register: %s' % e)
            data = dict(sylkrtc='error', transaction=transaction, error=str(e))
            self._send_data(json.dumps(data))
        except Exception, e:
            log.error('Unexpected error in account-register: %s' % e)
            data = dict(sylkrtc='error', transaction=transaction, error=str(e))
            self._send_data(json.dumps(data))

    def _OH_account_unregister(self, data):
        transaction = data.get('transaction', None)
        if transaction is None:
            log.warn('Transaction not specified!')
            return

        try:
            try:
                account = data['account']
            except KeyError:
                raise APIError('Invalid parameters: "account" must be specified')

            try:
                account_info = self.accounts_map[account]
            except KeyError:
                raise APIError('Unknown account specified: %s' % account)

            handle_id = account_info.janus_handle_id
            if handle_id is not None:
                block_on(self.backend.janus_detach(self.janus_session_id, handle_id))
                self.backend.janus_set_event_handler(handle_id, None)
                account_info.janus_handle_id = None
                self.account_handles_map.pop(handle_id)

            data = dict(sylkrtc='ack', transaction=transaction)
            self._send_data(json.dumps(data))
            log.msg('Account %s will unregister' % account)
        except APIError, e:
            log.error('account-unregister: %s' % e)
            data = dict(sylkrtc='error', transaction=transaction, error=str(e))
            self._send_data(json.dumps(data))
        except Exception, e:
            log.error('Unexpected error in account-unregister: %s' % e)
            data = dict(sylkrtc='error', transaction=transaction, error=str(e))
            self._send_data(json.dumps(data))

    def _OH_session_create(self, data):
        transaction = data.get('transaction', None)
        if transaction is None:
            log.warn('Transaction not specified!')
            return

        try:
            try:
                account = data['account']
                session = data['session']
                uri = data['uri']
                sdp = data['sdp']
            except KeyError:
                raise APIError('Invalid parameters: "account", "session", "uri" and "sdp" must be specified')

            try:
                account_info = self.accounts_map[account]
            except KeyError:
                raise APIError('Unknown account specified: %s' % account)

            if session in self.sessions_map:
                raise APIError('Session ID (%s) already in use' % session)

            # Create a new plugin handle and 'register' it, without actually doing so
            handle_id = block_on(self.backend.janus_attach(self.janus_session_id, 'janus.plugin.sip'))
            self.backend.janus_set_event_handler(handle_id, self._handle_janus_event)
            try:
                proxy = self._lookup_sip_proxy(account_info.id)
            except DNSLookupError:
                block_on(self.backend.janus_detach(self.janus_session_id, handle_id))
                self.backend.janus_set_event_handler(handle_id, None)
                raise APIError('DNS lookup error')
            account_uri = 'sip:%s' % account_info.id
            data = {'request': 'register', 'username': account_uri, 'ha1_secret': account_info.password, 'proxy': proxy, 'send_register': False}
            block_on(self.backend.janus_message(self.janus_session_id, handle_id, data))

            session_info = JanusSessionInfo(session)
            session_info.janus_handle_id = handle_id
            session_info.init_outgoing(account, uri)
            self.sessions_map[session_info.id] = session_info
            self.session_handles_map[handle_id] = session_info

            data = {'request': 'call', 'uri': 'sip:%s' % SIP_PREFIX_RE.sub('', uri)}
            jsep = {'type': 'offer', 'sdp': sdp}
            block_on(self.backend.janus_message(self.janus_session_id, handle_id, data, jsep))
            data = dict(sylkrtc='ack', transaction=transaction)
            self._send_data(json.dumps(data))
            log.msg('Outgoing session %s from %s to %s created' % (session, account, uri))
        except APIError, e:
            log.error('session-create: %s' % e)
            data = dict(sylkrtc='error', transaction=transaction, error=str(e))
            self._send_data(json.dumps(data))
        except Exception, e:
            log.error('Unexpected error in session-create: %s' % e)
            data = dict(sylkrtc='error', transaction=transaction, error=str(e))
            self._send_data(json.dumps(data))

    def _OH_session_answer(self, data):
        transaction = data.get('transaction', None)
        if transaction is None:
            log.warn('Transaction not specified!')
            return

        try:
            try:
                session = data['session']
                sdp = data['sdp']
            except KeyError:
                raise APIError('Invalid parameters: "session" and "sdp" must be specified')

            try:
                session_info = self.sessions_map[session]
            except KeyError:
                raise APIError('Unknown session specified: %s' % session)

            if session_info.direction != 'incoming':
                raise APIError('Cannot answer outgoing session')
            if session_info.state != 'connecting':
                raise APIError('Invalid state for session answer')

            data = {'request': 'accept'}
            jsep = {'type': 'answer', 'sdp': sdp}
            block_on(self.backend.janus_message(self.janus_session_id, session_info.janus_handle_id, data, jsep))
            data = dict(sylkrtc='ack', transaction=transaction)
            self._send_data(json.dumps(data))
            log.msg('%s answered session %s' % (session_info.account_id, session))
        except APIError, e:
            log.error('session-answer: %s' % e)
            data = dict(sylkrtc='error', transaction=transaction, error=str(e))
            self._send_data(json.dumps(data))
        except Exception, e:
            log.error('Unexpected error in session-answer: %s' % e)
            data = dict(sylkrtc='error', transaction=transaction, error=str(e))
            self._send_data(json.dumps(data))

    def _OH_session_trickle(self, data):
        transaction = data.get('transaction', None)
        if transaction is None:
            log.warn('Transaction not specified!')
            return

        try:
            try:
                session = data['session']
                candidates = data['candidates']
            except KeyError:
                raise APIError('Invalid parameters: "session" and "candidates" must be specified')

            try:
                session_info = self.sessions_map[session]
            except KeyError:
                raise APIError('Unknown session specified: %s' % session)
            if session_info.state == 'terminated':
                raise APIError('Session is terminated')

            block_on(self.backend.janus_trickle(self.janus_session_id, session_info.janus_handle_id, candidates))
            data = dict(sylkrtc='ack', transaction=transaction)
            self._send_data(json.dumps(data))
            log.msg('Trickled ICE candidate(s) for session %s' % session)
        except APIError, e:
            log.error('session-trickle: %s' % e)
            data = dict(sylkrtc='error', transaction=transaction, error=str(e))
            self._send_data(json.dumps(data))
        except Exception, e:
            log.error('Unexpected error in session-trickle: %s' % e)
            data = dict(sylkrtc='error', transaction=transaction, error=str(e))
            self._send_data(json.dumps(data))

    def _OH_session_terminate(self, data):
        transaction = data.get('transaction', None)
        if transaction is None:
            log.warn('Transaction not specified!')
            return

        try:
            try:
                session = data['session']
            except KeyError:
                raise APIError('Invalid parameters: "session" must be specified')

            try:
                session_info = self.sessions_map[session]
            except KeyError:
                raise APIError('Unknown session specified: %s' % session)
            if session_info.state not in ('connecting', 'progress', 'accepted', 'established'):
                raise APIError('Invalid state for session terminate: \"%s\"' % session_info.state)

            if session_info.direction == 'incoming' and session_info.state == 'connecting':
                data = {'request': 'decline', 'code': 486}
            else:
                data = {'request': 'hangup'}
            block_on(self.backend.janus_message(self.janus_session_id, session_info.janus_handle_id, data))
            data = dict(sylkrtc='ack', transaction=transaction)
            self._send_data(json.dumps(data))
            log.msg('%s terminated session %s' % (session_info.account_id, session))
        except APIError, e:
            log.error('session-terminate: %s' % e)
            data = dict(sylkrtc='error', transaction=transaction, error=str(e))
            self._send_data(json.dumps(data))
        except Exception, e:
            log.error('Unexpected error in session-terminate: %s' % e)
            data = dict(sylkrtc='error', transaction=transaction, error=str(e))
            self._send_data(json.dumps(data))

    # Event handlers

    def _OH_janus_event(self, data):
        handle_id = data['handle_id']
        event_type = data['event_type']
        event = data['event']

        if event_type == 'event':
            event_data = event['plugindata']['data']
            if 'result' in event_data:
                jsep = event.get('jsep', None)
                event_type = event_data['result']['event']
                if event_type in ('registering', 'registered', 'registration_failed', 'incomingcall'):
                    # skip 'registered' events from session handles
                    if event_type == 'registered' and event_data['result']['register_sent'] in (False, 'false'):
                        return
                    # account event
                    try:
                        account_info = self.account_handles_map[handle_id]
                    except KeyError:
                        log.warn('Could not find account for handle ID %s' % handle_id)
                        return
                    if event_type == 'incomingcall':
                        originator_uri = SIP_PREFIX_RE.sub('', event_data['result']['username'])
                        originator_display_name = event_data['result'].get('displayname', '').replace('"', '')
                        jsep = event.get('jsep', None)
                        assert jsep is not None
                        session_id = uuid.uuid4().hex
                        session = JanusSessionInfo(session_id)
                        session.janus_handle_id = handle_id
                        session.init_incoming(account_info.id, originator_uri, originator_display_name)
                        self.sessions_map[session_id] = session
                        self.session_handles_map[handle_id] = session
                        data = dict(sylkrtc='account_event',
                                    account=account_info.id,
                                    session=session_id,
                                    event='incoming_session',
                                    data=dict(originator=session.remote_identity.__dict__, sdp=jsep['sdp']))
                        log.msg('Incoming session %s %s <-> %s created' % (session.id,
                                                                           session.remote_identity.uri,
                                                                           session.local_identity.uri))
                    else:
                        registration_state = event_type
                        if registration_state == 'registration_failed':
                            registration_state = 'failed'
                        if account_info.registration_state == registration_state:
                            return
                        account_info.registration_state = registration_state
                        registration_data = dict(state=registration_state)
                        if registration_state == 'failed':
                            code = event_data['result']['code']
                            reason = event_data['result']['reason']
                            registration_data['reason'] = '%d %s' % (code, reason)
                        data = dict(sylkrtc='account_event',
                                    account=account_info.id,
                                    event='registration_state',
                                    data=registration_data)
                        log.msg('Account %s registration state changed to %s' % (account_info.id, registration_state))
                    self._send_data(json.dumps(data))
                elif event_type in ('calling', 'accepted', 'hangup'):
                    # session event
                    try:
                        session_info = self.session_handles_map[handle_id]
                    except KeyError:
                        log.warn('Could not find session for handle ID %s' % handle_id)
                        return
                    if event_type == 'hangup' and session_info.state == 'terminated':
                        return
                    if event_type == 'calling':
                        session_info.state = 'progress'
                    elif event_type == 'accepted':
                        session_info.state = 'accepted'
                    elif event_type == 'hangup':
                        session_info.state = 'terminated'
                    data = dict(sylkrtc='session_event',
                                session=session_info.id,
                                event='state',
                                data=dict(state=session_info.state))
                    log.msg('%s session %s state: %s' % (session_info.direction.title(), session_info.id, session_info.state))
                    if session_info.state == 'accepted' and session_info.direction == 'outgoing':
                        assert jsep is not None
                        data['data']['sdp'] = jsep['sdp']
                    elif session_info.state == 'terminated':
                        code = event_data['result'].get('code', 0)
                        reason = event_data['result'].get('reason', 'Unknown')
                        reason = '%d %s' % (code, reason)
                        data['data']['reason'] = reason
                    self._send_data(json.dumps(data))
                    if session_info.state == 'terminated':
                        self._cleanup_session(session_info)
                        log.msg('%s session %s %s <-> %s terminated (%s)' % (session_info.direction.title(),
                                                                             session_info.id,
                                                                             session_info.local_identity.uri,
                                                                             session_info.remote_identity.uri,
                                                                             reason))
                        # check if missed incoming call
                        if session_info.direction == 'incoming' and code == 487:
                            data = dict(sylkrtc='account_event',
                                        account=session_info.account_id,
                                        event='missed_session',
                                        data=dict(originator=session_info.remote_identity.__dict__))
                            log.msg('Incoming session from %s missed' % session_info.remote_identity.uri)
                            self._send_data(json.dumps(data))
                elif event_type == 'missed_call':
                    try:
                        account_info = self.account_handles_map[handle_id]
                    except KeyError:
                        log.warn('Could not find account for handle ID %s' % handle_id)
                        return
                    originator_uri = SIP_PREFIX_RE.sub('', event_data['result']['caller'])
                    originator_display_name = event_data['result'].get('displayname', '').replace('"', '')
                    # We have no session, so create an identity object by hand
                    originator = SessionPartyIdentity(originator_uri, originator_display_name)
                    data = dict(sylkrtc='account_event',
                                account=account_info.id,
                                event='missed_session',
                                data=dict(originator=originator.__dict__))
                    log.msg('Incoming session from %s missed' % originator.uri)
                    self._send_data(json.dumps(data))
                elif event_type in ('ack', 'declining', 'hangingup'):
                    # ignore
                    pass
                else:
                    log.warn('Unexpected SIP plugin event type: %s' % event_type)
            else:
                log.warn('Unexpected event: %s' % event)
        elif event_type == 'webrtcup':
            try:
                session_info = self.session_handles_map[handle_id]
            except KeyError:
                log.msg('Could not find session for handle ID %s' % handle_id)
                return
            session_info.state = 'established'
            data = dict(sylkrtc='session_event',
                        session=session_info.id,
                        event='state',
                        data=dict(state=session_info.state))
            log.msg('%s session %s state: %s' % (session_info.direction.title(), session_info.id, session_info.state))
            self._send_data(json.dumps(data))
        elif event_type == 'hangup':
            try:
                session_info = self.session_handles_map[handle_id]
            except KeyError:
                log.msg('Could not find session for handle ID %s' % handle_id)
                return
            if session_info.state != 'terminated':
                session_info.state = 'terminated'
                code = event.get('code', 0)
                reason = event.get('reason', 'Unknown')
                reason = '%d %s' % (code, reason)
                data = dict(sylkrtc='session_event',
                            session=session_info.id,
                            event='state',
                            data=dict(state=session_info.state, reason=reason))
                log.msg('%s session %s state: %s' % (session_info.direction.title(), session_info.id, session_info.state))
                self._send_data(json.dumps(data))
                self._cleanup_session(session_info)
                log.msg('%s session %s %s <-> %s terminated (%s)' % (session_info.direction.title(),
                                                                     session_info.id,
                                                                     session_info.local_identity.uri,
                                                                     session_info.remote_identity.uri,
                                                                     reason))
        elif event_type in ('media', 'detached'):
            # ignore
            pass
        else:
            log.warn('Received unexpected event type: %s' % event_type)


class SylkWebSocketServerFactory(WebSocketServerFactory):
    implements(IObserver)

    protocol = SylkWebSocketServerProtocol
    connections = set()
    backend = None    # assigned by WebHandler

    def __init__(self, *args, **kw):
        super(SylkWebSocketServerFactory, self).__init__(*args, **kw)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='JanusBackendDisconnected')

    def buildProtocol(self, addr):
        protocol = self.protocol()
        protocol.factory = self
        protocol.backend = self.backend
        return protocol

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_JanusBackendDisconnected(self, notification):
        for conn in self.connections.copy():
            conn.failConnection()