# Dot

This is the Discord bot used to organize and supervise live games on the [Unofficial Blood on the Clocktower server](https://discord.gg/botc).

## Setup

1. Install a recent version of [Python](https://python.org/).
1. (Optional) Create a virtualenv to store dependencies. See [Python documentation](https://docs.python.org/3/library/venv.html) for how to do this.
1. Install requirements. `pip install -r requirements.txt`
1. Create a Discord application on the [developer portal](https://discord.com/developers/applications). It needs the message content privileged gateway intent, which can be configured under Bot > Privileged Gateawy Intents.
1. On the Installation page, add the `bot` scope and the "Send Messages" and "View Channels" permissions, at a minimum.
1. Using the install link from the developer portal (under Installation), install the app on the server which contains the game chat channels mentioned in `.env`. You'll need to first ensure 
1. On the Bot page, generate a token. Do not share it.
1. Copy `.env.example` to `.env` and populate the variables there (notably including `DISCORD_TOKEN` from the previous step). (Alternatively, these can be given in environment variables.)

## Usage

The bot can be launched with Python. It accesses .env in the current working directory, and stores some data files in this directory.

```
python "Dot 3 Github.py"
```

## Spy module

The `townsquare_spy` module monitors ongoing games. It includes bot integration, but also a command-line tool to monitor a single game. It can be invoked as:

```
python -m townsquare_spy "https://clocktower.online/#game"
```

## Testing

The unit tests can be run by simply invoking `pytest`.

```
pytest
```