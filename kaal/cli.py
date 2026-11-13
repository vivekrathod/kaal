"""Command-line entry point for Kaal.

With no server configured, commands work directly on the local vault. With a
server URL configured (see kaal.client), the same commands go to kaal-server.
"""
from __future__ import annotations

from kaal import client, core


def main(argv: list[str] | None = None) -> None:
    args = core.build_parser().parse_args(argv)
    try:
        remote = client.remote_config()
    except client.ClientConfigError as error:
        core.die(str(error))
    if remote is None:
        args.func(args)
    else:
        client.run(remote, args)


if __name__ == "__main__":
    main()
