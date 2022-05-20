"""The file holding client connection patterns for noteable APIs."""

import asyncio
import functools
import os
from asyncio import Future
from collections import defaultdict
from datetime import datetime
from queue import LifoQueue
from typing import Optional, Type, Union, List
from uuid import UUID, uuid4

import httpx
import jwt
import structlog
import websockets
from pydantic import BaseModel, BaseSettings, ValidationError, parse_raw_as

from .types.deltas import FileDeltaAction, FileDeltaType, V2CellContentsProperties
from .types.kernels import SessionDetails, SessionRequestDetails, KernelRequestDetails, KernelRequestMetadata
from .types.files import NotebookFile
from .types.rtu import (
    RTU_ERROR_HARD_MESSAGE_TYPES,
    RTU_MESSAGE_TYPES,
    AuthenticationReply,
    AuthenticationRequest,
    AuthenticationRequestData,
    CallbackTracker,
    CellContentsDeltaReply,
    FileSubscribeReplySchema,
    GenericRTUMessage,
    GenericRTUReply,
    GenericRTUReplySchema,
    GenericRTURequest,
    GenericRTURequestSchema,
    MinimalErrorSchema,
    PingReply,
    PingRequest,
    RTUEventCallable,
    TopicActionReplyData,
)

logger = structlog.get_logger('noteable.' + __name__)


class ClientSettings(BaseSettings):
    """A pydantic settings object for loading settings into dataclasses"""

    auth0_config_path: str = "./auth0_config"


class ClientConfig(BaseModel):
    """Captures the client's config object for user settable arguments"""

    client_id: str = ""
    client_secret: str = ""
    domain: str = "app.noteable.world"
    backend_path: str = "gate/api/"
    auth0_domain: str = ""
    audience: str = "https://apps.noteable.world/gate"
    ws_timeout: int = 10


class Token(BaseModel):
    """Represents an oauth token response object"""

    access_token: str
    iss: str = None
    sub: str = None
    aud: str = None
    iat: datetime = None
    exp: datetime = None
    azp: str = None
    gty: str = None


class NoteableClient(httpx.AsyncClient):
    """An async client class that provides interfaces for communicating with Noteable APIs."""

    def _requires_ws_context(func):
        """A helper for checking if one is in a websocket context or not"""

        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            if self.rtu_socket is None:
                raise ValueError("Cannot send RTU request outside of a context manager scope.")
            return await func(self, *args, **kwargs)

        return wrapper

    def _default_timeout_arg(func):
        """A helper for checking if one is in a websocket context or not"""

        @functools.wraps(func)
        async def wrapper(self, *args, timeout=None, **kwargs):
            if timeout is None:
                timeout = self.config.ws_timeout
            return await func(self, *args, timeout=timeout, **kwargs)

        return wrapper

    def __init__(
        self,
        api_token: Optional[Union[str, Token]] = None,
        config: Optional[ClientConfig] = None,
        follow_redirects=True,
        **kwargs,
    ):
        """Initializes httpx client and sets up state trackers for async comms."""
        if not config:
            settings = ClientSettings()
            if not os.path.exists(settings.auth0_config_path):
                logger.error(
                    f"No config object passed in and no config file found at {settings.auth0_config_path}"
                    ", using default empty config"
                )
                config = ClientConfig()
            else:
                config = ClientConfig.parse_file(settings.auth0_config_path)
        self.config = config

        self.user = None
        self.token = api_token or self.get_token()
        if isinstance(self.token, str):
            self.token = Token(access_token=api_token)
        self.rtu_socket = None
        self.process_task_loop = None

        headers = kwargs.pop('headers', {})
        headers['Authorization'] = f"Bearer {self.token.access_token}"
        # assert 'Authorization' in headers, "No API token present for authenticating requests"

        # Set of active channel subscriptions (always subscribed to system messages)
        self.subscriptions = {'system'}
        # channel -> message_type -> callback_queue
        self.type_callbacks = defaultdict(lambda: defaultdict(LifoQueue))
        # channel -> transaction_id -> callback_queue
        self.transaction_callbacks = defaultdict(lambda: defaultdict(LifoQueue))
        super().__init__(
            base_url=f"https://{self.config.domain}/",
            follow_redirects=follow_redirects,
            headers=headers,
            **kwargs,
        )

    @property
    def origin(self):
        """Formates the domain in an origin string for websocket headers."""
        return f'https://{self.config.domain}'

    @property
    def ws_uri(self):
        """Formats the websocket URI out of the notable domain name."""
        return f"wss://{self.config.domain}/gate/api/v1/rtu"

    @property
    def api_server_uri(self):
        """Formats the websocket URI out of the notable domain name."""
        return f"https://{self.config.domain}/gate/api"

    def get_token(self):
        """Fetches and api token using oauth client config settings.

        WARNING: This is a blocking call so we can call it from init, but it should be quick
        """
        url = f"https://{self.config.auth0_domain}/oauth/token"
        data = {
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "audience": self.config.audience,
            "grant_type": "client_credentials",
        }
        resp = httpx.post(url, json=data)
        resp.raise_for_status()

        token = resp.json()["access_token"]
        token_data = jwt.decode(token, options={"verify_signature": False})
        return Token(access_token=token, **token_data)

    async def get_notebook(self, file_id) -> NotebookFile:
        """Fetches a notebook file via the Noteable REST API as a NotebookFile model (see files.py)"""
        resp = await self.get(f"{self.api_server_uri}/files/{file_id}")
        resp.raise_for_status()
        return NotebookFile.parse_raw(resp.content)

    async def get_kernel_session(self, file: Union[UUID, NotebookFile]) -> Optional[SessionDetails]:
        """Fetches the first notebook kernel session via the Noteable REST API.
        Returns None if no session is active.
        """
        file_id = file if not isinstance(file, NotebookFile) else file.id
        resp = await self.get(f"{self.api_server_uri}/files/{file_id}/sessions")
        resp.raise_for_status()
        logger.error(resp.content)
        sessions = parse_raw_as(List[SessionDetails], resp.content)
        if sessions:
            return sessions[0]

    async def launch_kernel_session(self, file: NotebookFile, kernel_name: Optional[str]=None, hardware_size: Optional[str]=None) -> SessionDetails:
        """Requests that a notebook session be launched via the Noteable REST API"""
        session = SessionRequestDetails.generate_file_request(file, kernel_name=kernel_name, hardware_size=hardware_size)
        # Needs the .dict conversion to avoid thinking it's an object with a syncronous byte stream
        resp = await self.post(f"{self.api_server_uri}/sessions", data=session.json())
        resp.raise_for_status()
        return SessionDetails.parse_raw(resp.content)

    async def __aenter__(self):
        """
        Creates an async test client for the Noteable App.

        An Authorization Bearer header must be present or Noteable
        returns a 403 immediately. Normally that bearer token is
        JWT format and the verify_jwt_token Security function would
        validate and extract principal-user-id from the token.
        """
        res = await httpx.AsyncClient.__aenter__(self)
        # Origin is needed or the server request crashes and rejects the connection
        headers = {'Authorization': self.headers['authorization'], 'Origin': self.origin}
        # raise ValueError(self.headers)
        self.rtu_socket = await websockets.connect(self.ws_uri, extra_headers=headers)
        # Loop indefinitely over the incoming websocket messages
        self.process_task_loop = asyncio.create_task(self._process_messages())
        # Ping to prove we're readily connected (enable if trying to determine if connecting vs auth is a problem)
        # await self.ping_rtu()
        # Authenticate for more advanced API calls
        await self.authenticate()
        return res

    async def __aexit__(self, exc_type, exc, tb):
        """Cleans out the tracker states and closes the httpx + websocket contexts."""
        try:
            if self.process_task_loop:
                self.process_task_loop.cancel()
                self.process_task_loop = None
            if self.rtu_socket:
                await self.rtu_socket.close()
                self.rtu_socket = None
            self.subscriptions = {'system'}
            # channel -> message_type -> callback_queue
            self.type_callbacks = defaultdict(lambda: defaultdict(LifoQueue))
            # channel -> transaction_id -> callback_queue
            self.transaction_callbacks = defaultdict(lambda: defaultdict(LifoQueue))
        except Exception:
            logger.exception("Error in closing out nested context loops")
        finally:
            return await httpx.AsyncClient.__aexit__(self, exc_type, exc, tb)

    def register_message_callback(
        self,
        callable: RTUEventCallable,
        channel: str,
        message_type: Optional[str] = None,
        transaction_id: Optional[UUID] = None,
        once: bool = True,
        response_schema: Optional[Type[BaseModel]] = None,
    ) -> CallbackTracker:
        """Registers a callback function that will be executed upon receiving the
        given message event type or transaction id in the specified topic channel.

        Multiple callbacks can exist against each callable.

        The once flag will indicate this callback should only be used for the next
        event trigger (default True).
        """
        tracker = CallbackTracker(
            once=once,
            count=0,
            callable=callable,
            channel=channel,
            message_type=message_type,
            transaction_id=transaction_id,
            next_trigger=Future(),
            response_schema=response_schema,
        )

        async def wrapped_callable(resp: GenericRTUMessage):
            """Wrapps the user callback function to handle message parsing and future triggers."""
            tracker.count += 1
            if resp.event in RTU_ERROR_HARD_MESSAGE_TYPES:
                resp = MinimalErrorSchema.parse_obj(resp)
                msg = resp.data['message']
                logger.exception(f"Request failed: {msg}")
                # TODO: Different exception class?
                tracker.next_trigger.set_exception(ValueError(msg))
            else:
                if tracker.response_schema:
                    resp = tracker.response_schema.parse_obj(resp)
                elif tracker.message_type in RTU_MESSAGE_TYPES:
                    resp = RTU_MESSAGE_TYPES[tracker.message_type].parse_obj(resp)

                try:
                    result = await callable(resp)
                except Exception as e:
                    logger.exception("Registered callback failed")
                    tracker.next_trigger.set_exception(e)
                else:
                    tracker.next_trigger.set_result(result)
            if not tracker.once:
                # Reset the next trigger promise
                tracker.next_trigger = Future()
                if tracker.transaction_id:
                    self.transaction_callbacks[tracker.channel][tracker.transaction_id].put_nowait(
                        tracker
                    )
                else:
                    self.type_callbacks[tracker.channel][tracker.message_type].put_nowait(tracker)

        # Replace the callable with a function that will manage itself and it's future awaitable
        tracker.callable = wrapped_callable
        if tracker.transaction_id:
            self.transaction_callbacks[channel][transaction_id].put_nowait(tracker)
        else:
            self.type_callbacks[channel][message_type].put_nowait(tracker)
        return tracker

    async def _process_messages(self):
        """Provides an infinite control loop for consuming RTU websocket messages.

        The loop will parse the message, convert to a RTUReply or RTURequest,
        log any validation errors (skipping callbacks), and finally identifying
        any callbacks that are registered to consume the given message and pass
        the message as the sole argument.
        """
        while True:
            # Release context control at the start of each loop
            await asyncio.sleep(0)
            try:
                msg = await self.rtu_socket.recv()
                if not isinstance(msg, str):
                    logger.exception(f"Unexepected message type found on socket: {type(msg)}")
                    continue
                try:
                    res = GenericRTUReply.parse_raw(msg)
                    channel = res.channel
                    event = res.event
                except ValidationError:
                    try:
                        res = GenericRTURequest.parse_raw(msg)
                        channel = res.channel
                        event = res.event
                    except ValidationError:
                        logger.exception(
                            f"Unexepected message found on socket: {msg[:30]}{'...' if len(msg) > 30 else ''}"
                        )
                        continue

                logger.debug(f"Received websocket message: {res}")
                # Check for transaction id responses
                id_lifo = self.transaction_callbacks[channel][res.transaction_id]
                while not id_lifo.empty():
                    tracker: CallbackTracker = id_lifo.get(block=False)
                    logger.debug(f"Found callable for {channel}/{tracker.transaction_id}")
                    await tracker.callable(res)
                type_lifo = self.type_callbacks[channel][event]
                # Check for general event callbacks
                while not type_lifo.empty():
                    tracker: CallbackTracker = type_lifo.get(block=False)
                    logger.debug(f"Found callable for {channel}/{event}")
                    await tracker.callable(res)

            except websockets.exceptions.ConnectionClosed:
                await asyncio.sleep(0)
                break
            except Exception:
                logger.exception("Unexpected callback failure")
                await asyncio.sleep(0)
                break

    @_requires_ws_context
    async def send_rtu_request(self, req: GenericRTURequestSchema):
        """Wraps converting a pydantic request model to be send down the websocket."""
        logger.debug(f"Sending websocket request: {req}")
        return await self.rtu_socket.send(req.json())

    @_requires_ws_context
    @_default_timeout_arg
    async def authenticate(self, timeout: float):
        """Authenticates a fresh websocket as the given user."""

        async def authorized(resp: AuthenticationReply):
            if resp.data.success:
                logger.debug("User is authenticated!")
                self.user = resp.data.user
            else:
                raise ValueError("Failed to authenticate websocket session")
            return resp

        # Register the transaction reply after sending the request
        req = AuthenticationRequest(
            transaction_id=uuid4(), data=AuthenticationRequestData(token=self.token.access_token)
        )
        tracker = AuthenticationReply.register_callback(self, req, authorized)
        await self.send_rtu_request(req)
        # Give it timeout seconds to respond
        return await asyncio.wait_for(tracker.next_trigger, timeout)

    @_requires_ws_context
    @_default_timeout_arg
    async def ping_rtu(self, timeout: float):
        """Sends a ping request to the RTU websocket and confirms the response is valid."""

        async def pong(resp: GenericRTUReply):
            """The pong response for pinging a webrowser"""
            logger.debug("Intial ping response received! Websocket is live.")
            return resp  # Do nothing, we just want to ensure we reach the event

        # Register the transaction reply after sending the request
        req = PingRequest(transaction_id=uuid4())
        tracker = PingReply.register_callback(self, req, pong)
        await self.send_rtu_request(req)
        # Give it timeout seconds to respond
        pong_resp = await asyncio.wait_for(tracker.next_trigger, timeout)
        # These should be consistent, but validate for good measure
        assert pong_resp.transaction_id == req.transaction_id
        assert pong_resp.event == "ping_reply"
        assert pong_resp.channel == "system"
        return pong_resp

    def _gen_subscription_request(self, channel: str):
        async def process_subscribe(resp: GenericRTUReplySchema[TopicActionReplyData]):
            if resp.data.success:
                self.subscriptions.add(resp.channel)
            else:
                logger.error(f"Failed to subscribe to channel topic: {channel}")
            return resp

        # Register the reply first
        tracker = self.register_message_callback(
            process_subscribe,
            channel,
            transaction_id=uuid4(),
            response_schema=GenericRTUReplySchema[TopicActionReplyData],
        )
        req = GenericRTURequest(
            transaction_id=tracker.transaction_id, event="subscribe_request", channel=channel
        )
        return req, tracker

    @_requires_ws_context
    @_default_timeout_arg
    async def subscribe_channel(self, channel: str, timeout: float):
        """A generic pattern for subscribing to topic channels."""
        req, tracker = self._gen_subscription_request(channel)
        await self.send_rtu_request(req)
        return await asyncio.wait_for(tracker.next_trigger, timeout)

    def files_channel(self, file_id):
        """Helper to build file channel names from file ids"""
        return f"files/{file_id}"

    @_requires_ws_context
    @_default_timeout_arg
    async def subscribe_file(
        self,
        file: Union[UUID, NotebookFile],
        timeout: float,
        from_version_id: Optional[UUID] = None,
    ):
        """Subscribes to a specified file for updates about it's contents."""
        if isinstance(file, NotebookFile):
            # TODO: Write test for file
            file_id = file.id
            # from_delta_id = file.last_save_delta_id
            from_version_id = file.current_version_id
        else:
            file_id = file
            # from_delta_id = from_delta_id
            from_version_id = file.current_version_id
        channel = self.files_channel(file_id)
        req, tracker = self._gen_subscription_request(channel)
        tracker.response_schema = FileSubscribeReplySchema
        # TODO: write test for these fields
        req.data = {}
        if from_version_id:
            req.data['from_version_id'] = from_version_id
        # TODO: Handle delta catchups?
        # if from_delta_id:
        #     req.data['from_delta_id'] = from_delta_id

        await self.send_rtu_request(req)
        return await asyncio.wait_for(tracker.next_trigger, timeout)

    @_requires_ws_context
    @_default_timeout_arg
    async def replace_cell_contents(
        self, file: NotebookFile, cell_id: UUID, contents: str, timeout: float
    ):
        """Sends an RTU request to replace the contents of a particular cell in a particular file."""

        async def check_success(resp: GenericRTUReplySchema[TopicActionReplyData]):
            if not resp.data.success:
                logger.error(f"Failed to submit cell change for file {file.id} -> {cell_id}")
            return resp

        req = file.generate_delta_request(
            uuid4(),
            FileDeltaType.cell_contents,
            FileDeltaAction.replace,
            cell_id,
            properties=V2CellContentsProperties(source=contents),
        )
        tracker = CellContentsDeltaReply.register_callback(self, req, check_success)
        await self.send_rtu_request(req)
        return await asyncio.wait_for(tracker.next_trigger, timeout)

    @_requires_ws_context
    @_default_timeout_arg
    async def execute(
        self,
        file: NotebookFile,
        cell_id: Optional[UUID],
        timeout: float,
        before_id: Optional[UUID] = None,
        after_id: Optional[UUID] = None,
    ):
        """Sends an RTU request to execute a part of the Notebook NotebookFile."""
        # TODO Confirm that kernel session is live first!
        assert not before_id or not after_id, 'Cannot define both a before_id and after_id'
        assert not cell_id or not after_id, 'Cannot define both a cell_id and after_id'
        assert not cell_id or not before_id, 'Cannot define both a cell_id and before_id'

        action = FileDeltaAction.execute_all
        if cell_id:
            action = FileDeltaAction.execute
        elif before_id:
            action = FileDeltaAction.execute_before
            cell_id = before_id
        elif after_id:
            action = FileDeltaAction.execute_after
            cell_id = after_id

        async def check_success(resp: GenericRTUReplySchema[TopicActionReplyData]):
            if not resp.data.success:
                logger.error(
                    f"Failed to submit execute request for file {file.id} -> {action}({cell_id})"
                )
            return resp

        req = file.generate_delta_request(
            uuid4(), FileDeltaType.cell_execute, action, cell_id, None
        )
        tracker = CellContentsDeltaReply.register_callback(self, req, check_success)
        await self.send_rtu_request(req)
        return await asyncio.wait_for(tracker.next_trigger, timeout)
