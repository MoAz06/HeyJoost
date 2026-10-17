"""Hey Joost: Dutch public data (BAG, CBS neighbourhood figures, school holidays, water and tides, weather, rain radar, electricity prices, NS with disruptions, bus and tram) as an MCP server."""

import argparse
import os

__version__ = "0.32.0"


def main() -> None:
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))

    parser = argparse.ArgumentParser(prog="nederland-mcp", description="Hey Joost: Nederlandse open data als MCP-server.")
    parser.add_argument("--http", action="store_true", help="draai als HTTP-server in plaats van via stdio")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"), help="adres (standaard 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")), help="poort (standaard 8000)")
    args = parser.parse_args()

    from .server import server

    if args.http:
        import uvicorn

        from .veilig import headers

        # As server.run("streamable-http") does, with the security headers around the whole app.
        app = server.streamable_http_app(stateless_http=True, json_response=True, host=args.host)
        uvicorn.run(headers(app), host=args.host, port=args.port, log_level=server.settings.log_level.lower(),
                    server_header=False)
    else:
        server.run()
