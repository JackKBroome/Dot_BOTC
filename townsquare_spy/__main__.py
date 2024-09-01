import argparse
import asyncio
import sys

from datetime import datetime, timezone
from .spy import *

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('url')
    args = parser.parse_args()

    def print_ts(message):
        ts = datetime.now(timezone.utc).strftime('\x1b[0;30m[%Y-%m-%d %H:%M:%S %Z]\x1b[0m')
        print(ts, message)

    player_id = random_player_id()
    socket_url, app_origin = interpret_url(args.url, player_id)
    session = Session()
    session.log = print_ts

    socket = connect_to_session(socket_url, origin=app_origin, player_id=player_id)
    async for m in socket:
        try:
            receive(session, m)
        except Exception as e:
            print('While processing', m, file=sys.stderr)
            raise

asyncio.run(main())