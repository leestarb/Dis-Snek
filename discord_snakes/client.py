import asyncio
import logging
import traceback
from random import randint
from typing import Coroutine, Optional, List, Dict

import aiohttp

from discord_snakes.const import logger_name
from discord_snakes.errors import WebSocketRestart, GatewayNotFound, WebSocketClosed, SnakeException
from discord_snakes.gateway import WebsocketClient
from discord_snakes.http_client import HTTPClient
from discord_snakes.models.discord_objects.guild import Guild
from discord_snakes.models.snowflake import Snowflake

log = logging.getLogger(logger_name)


class Snake:
    def __init__(
        self,
        intents,
        loop=None,
    ):
        self.loop: asyncio.AbstractEventLoop = asyncio.get_event_loop() if loop is None else loop
        self.intents = intents

        # "Factories"
        self.http: HTTPClient = HTTPClient(loop=self.loop)
        self.ws: WebsocketClient = WebsocketClient

        self._connection = None
        self._closed = False
        self._ready: asyncio.Event = asyncio.Event()

        # caches
        self.guilds_cache = set()

        self._listeners: Dict[str, List] = {}

        self.add_listener(self._on_raw_guild_create, "raw_guild_create")

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def latency(self) -> float:
        return self.ws.latency

    async def login(self, token):
        """
        Login to discord
        :param token: Your bots token
        """
        log.debug(f"Logging in with token: {token}")
        await self.http.login(token.strip())
        self.dispatch("login")
        await self._ws_connect()

    async def _ws_connect(self):
        params = {
            "http": self.http,
            "dispatch": self.dispatch,
            "intents": self.intents,
            "resume": False,
            "session_id": None,
            "sequence": None,
        }
        while not self.is_closed:
            log.info(f"Attempting to {'re' if params['resume'] else ''}connect to gateway...")

            try:
                self.ws = await self.ws.connect(**params)

                await self.ws.run()
            except WebSocketRestart as ex:
                # internally requested restart
                self.dispatch("disconnect")
                if ex.resume:
                    params.update(resume=True, session_id=self.ws.session_id, sequence=self.ws.sequence)
                    continue
                params.update(resume=False, session_id=None, sequence=None)

            except (OSError, GatewayNotFound, aiohttp.ClientError, asyncio.TimeoutError, WebSocketClosed) as ex:
                self.dispatch("disconnect")

                if isinstance(ex, WebSocketClosed):
                    if ex.code == 1000:
                        # clean close
                        return
                    elif ex.code == 4011:
                        raise SnakeException("Your bot is too large, you must use shards") from None
                    elif ex.code == 4013:
                        raise SnakeException("Invalid Intents have been passed") from None
                    elif ex.code == 4014:
                        raise SnakeException(
                            "You have requested privileged intents that have not been enabled or approved. Check the developer dashboard"
                        ) from None
                    raise

                if isinstance(ex, OSError) and ex.errno in (54, 10054):
                    print("should reconnect")
                    params.update(resume=True, session_id=self.ws.session_id, sequence=self.ws.sequence)
                    continue
                params.update(resume=False, session_id=None, sequence=None)

            except Exception as e:
                self.dispatch("disconnect")
                log.error("".join(traceback.format_exception(type(e), e, e.__traceback__)))
                params.update(resume=False, session_id=None, sequence=None)

            await asyncio.sleep(randint(1, 5))

    def start(self, token):
        self.loop.run_until_complete(self.login(token))

    def _queue_task(self, coro, event_name, *args, **kwargs):
        async def _async_wrap(_coro, _event_name, *_args, **_kwargs):
            try:
                await coro(*_args, **_kwargs)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.error(e)

        wrapped = _async_wrap(coro, event_name, *args, **kwargs)

        return asyncio.create_task(wrapped, name=f"snake:: {event_name}")

    def dispatch(self, event: str, *args, **kwargs):
        log.debug(f"Dispatching event: {event}")

        listeners = self._listeners.get(event, [])
        for _listen in listeners:
            try:
                self._queue_task(_listen, event, *args, **kwargs)
            except Exception as e:
                log.error(f"Error running listener: {e}")

    def add_listener(self, coro: Coroutine, event: Optional[str] = None):
        """
        Add a listener for an event, if no event is passed, one is determined
        :param coro: the coroutine to run
        :param event: the event to listen for
        :return:
        """
        if not event:
            event = coro.__name__

        event = event.replace("on_", "")

        if event not in self._listeners:
            self._listeners[event] = []
        self._listeners[event].append(coro)

    async def _on_raw_guild_create(self, data: dict):
        """
        Automatically cache a guild upon GUILD_CREATE event from gateway
        :param data: raw guild data
        """
        self.guilds_cache.add(Guild(data))

    async def get_guild(self, guild_id: Snowflake, with_counts: bool = False):
        g_data = await self.http.get_guild(guild_id, with_counts)
        return Guild(g_data)
